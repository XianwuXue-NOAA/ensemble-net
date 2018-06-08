#
# Copyright (c) 2018 Jonathan Weyn <jweyn@uw.edu>
#
# See the file LICENSE for your rights.
#

"""
Utilities for retrieving and processing METAR observation data using MesoWest. The main data structure is a dictionary
with elements for individual stations. Unfortunately conversion to an xarray Dataset is not possible because the minute
stamps for hourly METAR observations vary from site to site, making any aggregated time dimension unwieldy.
"""

from .MesoPy import Meso
import numpy as np
import pandas as pd
import pickle
import os
from ..util import meso_date_to_datetime


def _convert_variable_names(variables):
    parameter_dict = {
        'TMP2': 'air_temp',
        'DPT2': 'dew_point_temperature',
        'MSLP': 'altimeter',
        'VIS': 'visibility',
        'UGRD': 'wind_speed,wind_direction',
        'VGRD': 'wind_speed,wind_direction',
        'WGST': 'wind_gust',
        'ACPC': 'precip_accum_one_hour',
        'SNOL': 'precip_accum_one_hour',
        'CLD': 'cloud_layer_1_code,cloud_layer_2_code,cloud_layer_3_code'
    }
    if not(isinstance(variables, list) or isinstance(variables, tuple)):
        var_list = list(variables.split(','))
    else:
        var_list = variables
    new_var_list = []
    for v in var_list:
        try:
            new_var_list.append(parameter_dict[v])
        except KeyError:
            raise KeyError("'%s' is not a recognized variable available in MesoWest data" % v)
    var_string = ','.join(new_var_list)
    return var_string


def _cloud(series):
    """
    Changes the cloud code to a fractional coverage.
    """
    translator = {1: 0.,
                  2: 0.5,
                  3: 0.75,
                  4: 1.,
                  6: 0.25
                  }
    new_series = series.copy()
    for index, value in series.iteritems():
        try:
            new_value = translator[int(value % 10.)]
        except:
            new_value = 0.0
        new_series.loc[index] = new_value
    return new_series


def _reformat_data(data, start, end):
    """
    Re-formats the raw json data from MesoWest API call into pandas DataFrames.

    :param data: dict: result from Meso call
    :return: dict of pandas DataFrame objects where each key in the dict is a station
    """
    new_data = {}
    for station_data in data['STATION']:
        # Assign to DataFrame
        obs = pd.DataFrame(station_data['OBSERVATIONS'])

        # Convert column names to slightly more sane versions
        obs_var_names = data['STATION'][0]['SENSOR_VARIABLES']
        obs_var_keys = list(obs_var_names.keys())
        col_names = list(map(''.join, obs.columns.values))
        for c in range(len(col_names)):
            col = col_names[c]
            for k in range(len(obs_var_keys)):
                key = obs_var_keys[k]
                if col == list(obs_var_names[key].keys())[0]:
                    col_names[c] = key
        obs.columns = col_names

        # Get only hourly data
        minutes = []
        for row in obs.iterrows():
            date = row[1]['date_time']
            minutes.append(pd.to_datetime(date).minute)  # convert pd str to dt
        minute_count = np.bincount(np.array(minutes))
        rev_count = minute_count[::-1]
        minute_mode = minute_count.size - rev_count.argmax() - 1
        obs_hourly = obs[pd.DatetimeIndex(obs['date_time']).minute == minute_mode]

        # Reformat date to object
        date_obj = pd.to_datetime(obs_hourly['date_time'])
        obs_hourly.loc[:, 'date_time'] = date_obj
        obs_hourly = obs_hourly.set_index('date_time')

        # If we have precipitation or cloud, fix their missing values. For cloud, convert to cloud fraction.
        try:
            obs_hourly['precip_accum_one_hour'].fillna(0.0, inplace=True)
        except KeyError:
            pass
        try:
            obs_hourly['cloud_layer_1_code'].fillna(1.0, inplace=True)
            obs_hourly['cloud_layer_2_code'].fillna(1.0, inplace=True)
            obs_hourly['cloud_layer_3_code'].fillna(1.0, inplace=True)
            # Format cloud data
            cloud = 100. - 100. * (
                        (1 - _cloud(obs_hourly['cloud_layer_1_code'])) *
                        (1 - _cloud(obs_hourly['cloud_layer_2_code'])) *
                        (1 - _cloud(obs_hourly['cloud_layer_3_code'])))
            # Cloud exceeding 100% set to 100
            cloud[cloud > 100.] = 100.
            # Drop old cloud columns and replace with only total cloud
            obs_hourly = obs_hourly.drop('cloud_layer_1_code', axis=1)
            obs_hourly = obs_hourly.drop('cloud_layer_2_code', axis=1)
            obs_hourly = obs_hourly.drop('cloud_layer_3_code', axis=1)
            obs_hourly['CLD'] = cloud
        except KeyError:
            pass

        # Convert wind speed and direction to u and v
        if 'wind_speed' in col_names:
            obs_hourly['UGRD'] = -1. * obs_hourly['wind_speed'] * np.sin(obs_hourly['wind_direction'] * np.pi / 180.)
            obs_hourly['VGRD'] = -1. * obs_hourly['wind_speed'] * np.cos(obs_hourly['wind_direction'] * np.pi / 180.)

        # Convert the rest of the column names to the standard variable names
        rename_dict = {
            'air_temp': 'TMP2',
            'dew_point_temperature': 'DPT2',
            'altimeter': 'MSLP',
            'visibility': 'VIS',
            'wind_gust': 'WGST',
            'precip_accum_one_hour': 'ACPC'
        }
        obs_hourly = obs_hourly.rename(columns=rename_dict)

        # Remove any duplicate rows
        obs_hourly = obs_hourly[~obs_hourly.index.duplicated(keep='last')]

        # Re-index by hourly. Fills missing with NaNs. Try to interpolate the NaNs.
        expected_start = meso_date_to_datetime(start).replace(minute=minute_mode)
        expected_end = meso_date_to_datetime(end).replace(minute=minute_mode)
        expected_times = pd.date_range(expected_start, expected_end, freq='H').to_pydatetime()
        obs_hourly = obs_hourly.reindex(expected_times)
        obs_hourly = obs_hourly.interpolate(limit=2)

        # Assign to the grand dictionary
        new_data[station_data['STID']] = obs_hourly

    return new_data


def _reformat_metadata(metadata):
    new_data = {}
    for station_data in metadata['STATION']:
        new_data[station_data['STID']] = station_data
    return new_data


class MesoWest(Meso):
    """
    Wrapper class for retrieving, writing, and loading observation data from MesoWest. Supersedes the 'metadata',
    'timeseries' methods of MesoPy's Meso object to use customized parameters and return data in a concise format.
    """
    def __init__(self, token, root_directory=None):
        super(MesoWest, self).__init__(token)
        if root_directory is None:
            self._root_directory = '%s/.mesowest' % os.path.expanduser('~')
        else:
            self._root_directory = root_directory
        self.Data = None
        self.Metadata = None
        self.data_variables = []

    def lat(self, stations=None):
        if stations is None:
            try:
                stations = list(self.Metadata.keys())
            except AttributeError:
                raise AttributeError('Call to lat method is only valid after metadata are loaded.')
        try:
            lat = np.array([float(self.Metadata[s]['LATITUDE']) for s in stations])
            return lat
        except AttributeError:
            raise AttributeError('Call to lat method is only valid after metadata are loaded.')

    def lon(self, stations=None):
        if stations is None:
            try:
                stations = list(self.Metadata.keys())
            except AttributeError:
                raise AttributeError('Call to lon method is only valid after metadata are loaded.')
        try:
            lon = np.array([float(self.Metadata[s]['LONGITUDE']) for s in stations])
            return lon
        except AttributeError:
            raise AttributeError('Call to lon method is only valid after metadata are loaded.')

    def timeseries(self, start, end, **kwargs):
        if 'vars' in list(kwargs.keys()):
            kwargs['vars'] = _convert_variable_names(kwargs['vars'])
        ts = super(MesoWest, self).timeseries(start, end, **kwargs)
        return _reformat_data(ts, start, end)

    def metadata(self, **kwargs):
        meta = super(MesoWest, self).metadata(**kwargs)
        return _reformat_metadata(meta)

    def load(self, start, end, file=None, **kwargs):
        """
        Retrieves a timeseries of data with the specified start and end times, and kwargs passed to the MesoPy
        'timeseries' method. Loads the concise, formatted data to the instance's 'Data' attribute. If the optional
        kwarg 'file' is given, then searches first to load the data from that file, and otherwise retrieves data, then
        saves it to that file.

        :param start: str: starting date for MesoPy timeseries (YYYYMMDDHHMM)
        :param end: str: ending date for MesoPy timeseries (YYYYMMDDHHMM)
        :param file: str: optional file name to read and/or write data to (using pickle)
        :param kwargs: passed to MesoPy.Meso.timeseries
        :return:
        """
        if file is not None:
            if os.path.isfile(file):
                file_exists = True
                write_file = False
            elif os.path.isfile('%s/%s' % (self._root_directory, file)):
                file_exists = True
                write_file = False
                file = '%s/%s' % (self._root_directory, file)
            else:
                file_exists = False
                write_file = True
        else:
            file_exists = False
            write_file = False

        if file_exists:
            with open(file, 'rb') as handle:
                ts = pickle.load(handle)
        else:
            ts = self.timeseries(start, end, **kwargs)

        if write_file:
            with open(file, 'wb') as handle:
                pickle.dump(ts, handle, pickle.HIGHEST_PROTOCOL)

        self.Data = ts
        for station, df in ts.items():
            self.data_variables = list(set(self.data_variables + list(df.columns)))

    def load_metadata(self, **kwargs):
        """
        Loads station metadata from the MesoPy 'metadata' method into the instance's 'Metadata' attribute. Writes in a
        concise dictionary format that is useful for lookup of parameters like latitude/longitude.

        :param kwargs: passed to MesoPy.Meso.metadata
        :return:
        """
        meta = self.metadata(**kwargs)
        self.Metadata = meta