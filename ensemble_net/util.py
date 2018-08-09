#
# Copyright (c) 2017-18 Jonathan Weyn <jweyn@uw.edu>
#
# See the file LICENSE for your rights.
#

"""
Ensemble-net utilities.
"""

from datetime import datetime
import types
import pickle
import tempfile
from copy import deepcopy


# ==================================================================================================================== #
# General utility functions
# ==================================================================================================================== #

def make_keras_picklable():
    """
    Thanks to http://zachmoshe.com/2017/04/03/pickling-keras-models.html

    :return:
    """
    import keras.models

    def __getstate__(self):
        model_str = ""
        with tempfile.NamedTemporaryFile(suffix='.hdf5', delete=True) as fd:
            keras.models.save_model(self, fd.name, overwrite=True)
            model_str = fd.read()
        d = {'model_str': model_str}
        return d

    def __setstate__(self, state):
        with tempfile.NamedTemporaryFile(suffix='.hdf5', delete=True) as fd:
            fd.write(state['model_str'])
            fd.flush()
            model = keras.models.load_model(fd.name)
        self.__dict__ = model.__dict__

    cls = keras.models.Model
    cls.__getstate__ = __getstate__
    cls.__setstate__ = __setstate__


def get_object(module_class):
    """
    Given a string with a module class name, it imports and returns the class.
    This function (c) Tom Keffer, weeWX; modified by Jonathan Weyn.
    """
    # Split the path into its parts
    parts = module_class.split('.')
    # Get the top level module
    module = parts[0]  # '.'.join(parts[:-1])
    # Import the top level module
    mod = __import__(module)
    # Recursively work down from the top level module to the class name.
    # Be prepared to catch an exception if something cannot be found.
    try:
        for part in parts[1:]:
            module = '.'.join([module, part])
            # Import each successive module
            __import__(module)
            mod = getattr(mod, part)
    except ImportError as e:
        # Can't find a recursive module. Give a more informative error message:
        raise ImportError("'%s' raised when searching for %s" % (str(e), module))
    except AttributeError:
        # Can't find the last attribute. Give a more informative error message:
        raise AttributeError("Module '%s' has no attribute '%s' when searching for '%s'" %
                             (mod.__name__, part, module_class))

    return mod


def get_from_class(module_name, class_name):
    """
    Given a module name and a class name, return an object corresponding to the class retrieved as in
    `from module_class import class_name`.

    :param module_name: str: name of module (may have . attributes)
    :param class_name: str: name of class
    :return: object pointer to class
    """
    mod = __import__(module_name, fromlist=[class_name])
    class_obj = getattr(mod, class_name)
    return class_obj


def save_model(model, file_name):
    """
    Saves a class instance with a 'model' attribute to disk. Creates two files: one pickle file containing no model
    saved as ${file_name}.pkl and one for the model saved as ${file_name}.keras. Use the function load_model() to load
    a model saved with this method.

    :param model: model instance (with a 'model' attribute) to save
    :param file_name: str: base name of save files
    :return:
    """
    model.model.save('%s.keras' % file_name)
    model_copy = deepcopy(model)
    model_copy.model = None
    with open('%s.pkl' % file_name, 'wb') as f:
        pickle.dump(model_copy, f, protocol=pickle.HIGHEST_PROTOCOL)


def load_model(file_name):
    """
    Loads a model saved to disk with the 'save_model' method.

    :param file_name: str: base name of save files
    :return: model: loaded object
    """
    with open('%s.pkl' % file_name, 'rb') as f:
        model = pickle.load(f)
    model.model = keras.models.load_model('%s.keras' % file_name, compile=True)


# ==================================================================================================================== #
# Type conversion functions
# ==================================================================================================================== #

def date_to_datetime(date_str):
    """
    Converts a date from string format to datetime object.
    """
    if date_str is None:
        return
    if isinstance(date_str, str):
        return datetime.strptime(date_str, '%Y-%m-%d %H:%M')


def date_to_string(date):
    """
    Converts a date from datetime object to string format.
    """
    if date is None:
        return
    if not isinstance(date, str):
        return datetime.strftime(date, '%Y-%m-%d %H:%M')


def file_date_to_datetime(date_str):
    """
    Converts a string date from config formatting %Y%m%d to a datetime object.
    """
    if date_str is None:
        return
    if isinstance(date_str, str):
        return datetime.strptime(date_str, '%Y%m%d%H')


def date_to_file_date(date):
    """
    Converts a string date from config formatting %Y%m%d to a datetime object.
    """
    if date is None:
        return
    if not isinstance(date, str):
        return datetime.strftime(date, '%Y%m%d%H')


def meso_date_to_datetime(date_str):
    """
    Converts a string date from config formatting %Y%m%d to a datetime object.
    """
    if date_str is None:
        return
    if isinstance(date_str, str):
        return datetime.strptime(date_str, '%Y%m%d%H%M')


def date_to_meso_date(date):
    """
    Converts a string date from config formatting %Y%m%d to a datetime object.
    """
    if date is None:
        return
    if not isinstance(date, str):
        return datetime.strftime(date, '%Y%m%d%H%M')
