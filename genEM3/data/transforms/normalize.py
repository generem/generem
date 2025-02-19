"""Module for transformation which normalize the data to specific desired distributions"""
from torch import Tensor


class ToStandardNormal:
    """Normalize data to std:1 and mean: 0"""
    def __init__(self, mean: float = 136.0, std: float = 40.0):
        """Initialization: setting the mean and the standard deviation"""
        self.mean = mean
        self.std = std

    def __call__(self, input_data: Tensor):
        """ call method for class. Normalizes the data to mean==0 and std==1"""
        return (input_data - self.mean) / self.std


class ToZeroOneRange:
    """Normalize to [0,1] range"""
    def __init__(self, minimum: float = 0, maximum: float = 255):
        """Initialization: setting the minimum and maximum of data values"""
        self.minimum = minimum
        self.maximum = maximum

    def __call__(self, input_data: Tensor):
        """ call method for class. Limits data to [0, 1] range"""
        return (input_data - self.minimum) / (self.maximum-self.minimum)
