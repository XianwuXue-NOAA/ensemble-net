#!/usr/bin/env python3
#
# Copyright (c) 2017-18 Jonathan Weyn <jweyn@uw.edu>
#
# See the file LICENSE for your rights.
#

"""
Trains and tests an ensemble selection model using predictors generated by ens_sel_batch_process.py. Implements an
'online learning' scheme whereby chunks of the data are loaded dynamically and training occurs on these individual
chunks.
"""

from ensemble_net.util import save_model
from ensemble_net.ensemble_selection import preprocessing, model, verify
import numpy as np
import multiprocessing
import time
import xarray as xr
import os
import random
from shutil import copyfile


#%% User parameters

# Paths to important files
root_data_dir = '%s/Data/ensemble-net' % os.environ['WORKDIR']
predictor_file = '%s/predictors_201504-201603_28N40N100W78W_x4_no_c.nc' % root_data_dir
model_file = '%s/selector_201504-201603_no_c_MSLP' % root_data_dir
result_file = '%s/result_201504-201603_28N40N100W78W_x4_no_c_MSLP.nc' % root_data_dir
convolved = False

# Copy file to scratch space
copy_file_to_scratch = True

# Optionally predict for only a subset of variables. Must use integer index as a list, or 'all'
variables = [2]

# Neural network configuration and options
chunk_size = 10
batch_size = 50
scaler_fit_size = 100
epochs_per_chunk = 10
loops = 3
impute_missing = True
scale_targets = False
val = 'random'
val_size = 46
# Use multiple GPUs
n_gpu = 1


#%% End user configuration

# Parameter checks
if variables == 'all' or variables is None:
    ens_sel = {}
else:
    if type(variables) is not list and type(variables) is not tuple:
        try:
            variables = int(variables)
            variables = [variables]
        except (TypeError, ValueError):
            raise TypeError("'variables' must be a list of integers or 'all'")
    else:
        try:
            variables = [int(v) for v in variables]
        except (TypeError, ValueError):
            raise TypeError("indices in 'variables' must be integer types")
    ens_sel = {'obs_var': variables}


#%% Process

def process_chunk(ds, **sel):
    if len(sel) > 0:
        ds = ds.sel(**sel)
    forecast_predictors, fpi = preprocessing.convert_ensemble_predictors_to_samples(ds['ENS_PRED'].values,
                                                                                    convolved=convolved)
    ae_predictors, epi = preprocessing.convert_ae_meso_predictors_to_samples(ds['AE_PRED'].values, convolved=convolved)
    ae_targets, eti = preprocessing.convert_ae_meso_predictors_to_samples(np.expand_dims(ds['AE_TAR'].values, 3),
                                                                          convolved=convolved)
    combined_predictors = preprocessing.combine_predictors(forecast_predictors, ae_predictors)

    # Remove samples with NaN
    if impute_missing:
        p, t = combined_predictors, ae_targets
    else:
        p, t = preprocessing.delete_nan_samples(combined_predictors, ae_targets)

    return p, t


def subprocess_chunk(pid, ds, shared, **sel):
    print('Process %d (%s): loading new predictors...' % (pid, os.getpid()))
    p, t = process_chunk(ds, **sel)
    shared['p'] = p
    shared['t'] = t
    return shared


# Copy the file to scratch, if requested, and available
try:
    job_id = os.environ['SLURM_JOB_ID']
except KeyError:
    copy_file_to_scratch = False
if copy_file_to_scratch:
    predictor_file_name = predictor_file.split('/')[-1]
    scratch_file = '/scratch/%s/%s/%s' % (os.environ['USER'], os.environ['SLURM_JOB_ID'], predictor_file_name)
    print('Copying predictor file to scratch space...')
    copyfile(predictor_file, scratch_file)
    predictor_file = scratch_file


# Load a Dataset with the predictors
print('Opening predictor dataset %s...' % predictor_file)
predictor_ds = xr.open_dataset(predictor_file, mask_and_scale=True)
num_dates = predictor_ds.ENS_PRED.shape[0]
num_members = predictor_ds.AE_TAR.shape[2]
num_stations = predictor_ds.AE_TAR.shape[-1]
if ens_sel == {}:
    num_variables = predictor_ds.AE_TAR.shape[1]
else:
    num_variables = len(variables)


# Get the indices for a validation set
print('Processing validation set...')
if val == 'first':
    val_set = list(range(0, val_size))
    train_set = list(range(val_size, num_dates))
elif val == 'last':
    val_set = list(range(num_dates - val_size, num_dates))
    train_set = list(range(0, num_dates - val_size))
elif val == 'random':
    train_set = list(range(num_dates))
    val_set = []
    for j in range(val_size):
        i = random.choice(train_set)
        val_set.append(i)
        train_set.remove(i)
    val_set.sort()
else:
    raise ValueError("'val' must be 'first', 'last', or 'random'")

# Get chunks for online training
chunks = []
index = 0
while index < len(train_set):
    chunks.append(train_set[index:index + chunk_size])
    index += chunk_size

# Load the validation set
new_ds = predictor_ds.isel(init_date=val_set)
p_val, t_val = process_chunk(new_ds, **ens_sel)
input_shape = p_val.shape[1:]
num_outputs = t_val.shape[1]


# Build an ensemble selection model
print('Building an EnsembleSelector model...')
selector = model.EnsembleSelector(impute_missing=impute_missing, scale_targets=scale_targets)
layers = (
    # ('Conv2D', (64,), {
    #     'kernel_size': (3, 3),
    #     'activation': 'relu',
    #     'input_shape': input_shape
    # }),
    ('Dense', (1024,), {
        'activation': 'relu',
        'input_shape': input_shape
    }),
    ('Dropout', (0.25,), {}),
    ('Dense', (num_outputs,), {
        'activation': 'relu'
    }),
    ('Dropout', (0.25,), {}),
    ('Dense', (num_outputs,), {
        'activation': 'linear'
    })
)
# reduce_lr = ReduceLROnPlateau(monitor='val_loss', factor=0.5, patience=10, min_lr=0.001)
selector.build_model(layers=layers, gpus=n_gpu, loss='mse', optimizer='adam', metrics=['mae'])


# Initialize the model's Imputer and Scaler with a larger set of data
print('Fitting the EnsembleSelector Imputer and Scaler...')
fit_set = train_set[:scaler_fit_size]
new_ds = predictor_ds.isel(init_date=fit_set)
predictors, targets = process_chunk(new_ds, **ens_sel)
selector.init_fit(predictors, targets)


# Do the online training
# Train and evaluate the model
print('Training the EnsembleSelector model...')
start_time = time.time()
first_chunk = True
manager = multiprocessing.Manager()
shared_chunk = manager.dict()
history = []
for loop in range(loops):
    print('  Loop %d of %d' % (loop+1, loops))
    for chunk in range(len(chunks)):
        print('    Data chunk %d of %d' % (chunk+1, len(chunks)))
        predictors, targets, new_ds = (None, None, None)
        # If we're on the first chunk, we need to do an initial set of the predictors and targets. Afterwards, we
        # spawn a background process to load the next chunk while training on the current one.
        if first_chunk:
            new_ds = predictor_ds.isel(init_date=chunks[chunk])
            predictors, targets = process_chunk(new_ds, **ens_sel)
            first_chunk = False
        else:
            predictors, targets = (shared_chunk['p'].copy(), shared_chunk['t'].copy())
        # Process the next chunk
        next_chunk = (chunk + 1 if chunk < len(chunks) - 1 else 0)
        new_ds = predictor_ds.isel(init_date=chunks[next_chunk])
        process = multiprocessing.Process(target=subprocess_chunk, args=(1, new_ds, shared_chunk), kwargs=ens_sel)
        process.start()
        # Fit the Selector
        print('    Training...')
        hist = selector.fit(predictors, targets, batch_size=batch_size, epochs=epochs_per_chunk, initialize=False,
                            verbose=1, validation_data=(p_val, t_val))
        history.append(hist)
        # Wait for the background process to finish
        process.join()

end_time = time.time()

score = selector.evaluate(p_val, t_val, verbose=0)
print("\nTrain time -- %s seconds --" % (end_time - start_time))
print('Test loss:', score[0])
print('Test mean absolute error:', score[1])


# Save the model, if requested
if model_file is not None:
    print('Saving model to disk...')
    save_model(selector, model_file)


#%% Process the results

predicted = selector.predict(p_val)

# Reshape the prediction and the targets to meaningful dimensions
new_target_shape = (val_size, num_members, num_stations, num_variables)
predicted = predicted.reshape(new_target_shape)
t_test = t_val.reshape(new_target_shape)

# Create a Dataset for the results
result = xr.Dataset(
    coords={
        'time': predictor_ds['init_date'].isel(init_date=val_set),
        'member': predictor_ds.member,
        'variable': predictor_ds.obs_var.isel(**ens_sel),
        'station': range(num_stations)
    }
)

result['prediction'] = (('time', 'member', 'station', 'variable'), predicted)
result['target'] = (('time', 'member', 'station', 'variable'), t_test)

# Run the selection on the validation set
selector_scores = []
selector_ranks = []
verif_ranks = []
verif_scores = []
last_time_scores = []
last_time_ranks = []
for day in val_set:
    day_as_list = [day]
    print('\nDay %d:' % day)
    new_ds = predictor_ds.isel(init_date=day_as_list, **ens_sel)
    select_predictors, select_shape = preprocessing.format_select_predictors(new_ds.ENS_PRED.values,
                                                                             new_ds.AE_PRED.values,
                                                                             None, convolved=convolved, num_members=10)
    select_verif = verify.select_verification(new_ds.AE_TAR.values, select_shape,
                                              convolved=convolved, agg=verify.stdmean)
    select_verif_12 = verify.select_verification(new_ds.AE_PRED[:, :, :, [-1]].values, select_shape,
                                                 convolved=convolved, agg=verify.stdmean)
    selection = selector.select(select_predictors, select_shape, agg=verify.stdmean)
    selector_scores.append(selection[:, 0])
    selector_ranks.append(selection[:, 1])
    verif_scores.append(select_verif[:, 0])
    verif_ranks.append(select_verif[:, 1])
    last_time_scores.append(select_verif_12[:, 0])
    last_time_ranks.append(select_verif_12[:, 1])
    ranks = np.vstack((selection[:, 1], select_verif[:, 1], select_verif_12[:, 1])).T
    scores = np.vstack((selection[:, 0], select_verif[:, 0], select_verif_12[:, 0])).T
    print(ranks)
    print('Rank score of Selector: %f' % verify.rank_score(ranks[:, 0], ranks[:, 1]))
    print('Rank score of last-time estimate: %f' % verify.rank_score(ranks[:, 2], ranks[:, 1]))
    print('MSE of Selector score: %f' % np.mean((scores[:, 0] - scores[:, 1]) ** 2.))
    print('MSE of last-time estimate: %f' % np.mean((scores[:, 2] - scores[:, 1]) ** 2.))

result['selector_scores'] = (('time', 'member'), selector_scores)
result['selector_ranks'] = (('time', 'member'), selector_ranks)
result['verif_scores'] = (('time', 'member'), verif_scores)
result['verif_ranks'] = (('time', 'member'), verif_ranks)
result['last_time_scores'] = (('time', 'member'), last_time_scores)
result['last_time_ranks'] = (('time', 'member'), last_time_ranks)
result.to_netcdf(result_file)
