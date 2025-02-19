import os
import json
import random
from collections import namedtuple
from typing import Tuple, Sequence, List, Callable, Dict, NamedTuple, Union
from functools import lru_cache

import numpy as np
from scipy.interpolate import griddata
import matplotlib.pyplot as plt
import torch
from torch.utils.data import Dataset
import wkw
import pandas as pd

from genEM3.util.path import get_data_dir
from genEM3.data import transforms

np.random.seed(1337)

DataSourceDefaults = (
    ("id", str),
    ("input_path", 'NaN'),
    ("input_bbox", 'NaN'),
    ("input_mean", 'NaN'),
    ("input_std", 'NaN'),
    ("target_path", 'NaN'),
    ("target_bbox", 'NaN'),
    ("target_class", 'NaN'),
    ("target_binary", 'NaN'),
)

DataSource = namedtuple(
    'DataSource',
    [fields[0] for fields in list(DataSourceDefaults)],
    defaults=[defaults[1] for defaults in list(DataSourceDefaults)])

DataSplit = namedtuple(
    'DataSplit',
    ['train',
     'validation',
     'test']
)


class WkwData(Dataset):
    """Implements (2D/3D) pytorch Dataset subclass for wkw data"""

    def __init__(self,
                 input_shape: Tuple[int, int, int],
                 target_shape: Tuple[int, int, int],
                 data_sources: Sequence[DataSource],
                 data_split: DataSplit = None,
                 stride: Tuple[int, int, int] = None,
                 normalize: bool = True,
                 transforms: Callable = None,
                 pad_target: bool = False,
                 cache_RAM: bool = True,
                 cache_HDD: bool = False,
                 cache_HDD_root: str = None):

        """
                Args:
                    input_shape:
                        Specifies (x,y,z) dimensions of input patches in pixels
                    target_shape:
                        Specifies (x,y,z) dimensions of target patches in pixels
                    data_sources:
                        Sequence of `DataSource` named tuples defining a given wkw data source and bounding box. Can
                        either be defined directly or generated from a datasource json file using the static
                        `WkwData.datasources_from_json` method.
                        Example (direct):
                            data_sources = [
                                wkwdata.Datasource(id=1, input_path='/path/to/wkw/input1',
                                    input_bbox=[pos_x, pos_y, pos_z, ext_x, ext_y, ext_z], input_mean=148.0,
                                    input_std=36.0, target_path='/path/to/wkw/target1'),
                                    target_bbox=[pos_x, pos_y, pos_z, ext_x, ext_y, ext_z]),
                                wkwdata.Datasource(id=2, input_path='/path/to/wkw/input2',
                                    input_bbox=[pos_x, pos_y, pos_z, ext_x, ext_y, ext_z], input_mean=148.0,
                                    input_std=36.0, target_path='/path/to/wkw/target2'),
                                    target_bbox=[pos_x, pos_y, pos_z, ext_x, ext_y, ext_z])]
                        Example (json import):
                            data_sources = WkwData.datasources_from_json(datasources_json_path)
                    data_split:
                        Defines how provided data is split into training, validation and test sets. The split can either
                        be defined as strata (define specific data sources to serve as train, val, test sets) or
                        as fractions (define subsets of samples drawn from all data sources to serve as train, val, test
                        sets).
                        Example (strata):
                            To use data source ids (1,3,4) as train-, ids (2,6) as validation- and id 5 as test set:
                            data_split = wkwdata.DataSplit(train=[1,3,4], validation=[2,6], test=[5])
                        Example (fractions)
                            To use a 70% of the data as train, 20% as validation and 10% as test set:
                            data_split = wkwdata.DataSplit(train=0.7, validation=0.2, test=0.1)
                    normalize:
                        If true, patches are normalized to standard normal using input mean and std specified in
                        the respective datasource
                    pad_target:
                        If true, target patches are padded to the same shape as input patches
                    cache_RAM:
                        If true, all data is cached into RAM for faster training.
                    cache_HDD:
                        If true, all data is cached to HDD for faster training (Mostly relevant if data is hosted at a
                        remote location and bandwidth to local instance is limited or multiple processes need to access
                        the same path).
                    cache_HDD_root:
                        Path on the local filesystem where HDD cache should be created
                """

        if cache_HDD_root is None:
            cache_HDD_root = '.'

        self.input_shape = input_shape
        self.output_shape = target_shape
        self.data_sources = data_sources
        self.data_split = data_split

        if stride is None:
            self.stride = target_shape
        else:
            self.stride = stride

        self.normalize = normalize
        self.transforms = transforms
        self.pad_target = pad_target
        self.cache_RAM = cache_RAM
        self.cache_HDD = cache_HDD

        self.cache_HDD_root = cache_HDD_root
        self.data_cache_input = dict()
        self.data_cache_output = dict()
        self.data_meshes = []
        self.data_inds_min = []
        self.data_inds_max = []

        self.data_train_inds = []
        self.data_validation_inds = []
        self.data_test_inds = []

        self.get_data_meshes()
        self.get_data_ind_ranges()

        if data_split is not None:
            self.get_data_ind_splits()

        if cache_RAM | cache_HDD:
            self.fill_caches()

    def __len__(self):
        """This method returns the length of the dataset"""
        return self.data_inds_max[-1] + 1

    def __getitem__(self, idx):
        """indexing the dataset calls this method"""
        return self.get_ordered_sample(idx)

    def get_data_meshes(self):
        """ Return the meshgrid for each datasource in self.datasources """
        [self.data_meshes.append(self.get_data_mesh(i)) for i in range(len(self.data_sources))]

    def get_data_mesh(self, data_source_idx):
        """ Returns the grid of x, y and z locations for an individual datasource"""
        corner_min_target = np.floor(np.asarray(self.data_sources[data_source_idx].input_bbox[0:3]) +
                                    np.asarray(self.input_shape) / 2).astype(int)
        n_fits = np.floor((np.asarray(self.data_sources[data_source_idx].input_bbox[3:6]) -
                           np.asarray(self.input_shape)) / np.asarray(self.stride)).astype(int) + 1
        corner_max_target = corner_min_target + (n_fits - 1) * np.asarray(self.stride)
        x = np.arange(corner_min_target[0], corner_max_target[0] + self.stride[0], self.stride[0])
        y = np.arange(corner_min_target[1], corner_max_target[1] + self.stride[1], self.stride[1])
        z = np.arange(corner_min_target[2], corner_max_target[2] + self.stride[2], self.stride[2])
        xm, ym, zm = np.meshgrid(x, y, z)
        mesh_target = {'x': xm, 'y': ym, 'z': zm}
        mesh_input = {'x': xm, 'y': ym, 'z': zm}

        return {'input': mesh_input, 'target': mesh_target}

    def get_data_ind_ranges(self):

        """ Computes the global linear idx limits contained in the respective training data cubes"""
        for source_idx, _ in enumerate(range(len(self.data_sources))):
            if source_idx == 0:
                # Minimum index is 0 for the initial group 
                self.data_inds_min.append(0)
            else:
                # The minimum of all subsequent data sources is the maximum of the previous 
                # group plus 1
                self.data_inds_min.append(self.data_inds_max[source_idx - 1] + 1)
            # The maximum index is the minimum + length of each data source minus 1
            self.data_inds_max.append(self.data_inds_min[source_idx] +
                                      self.data_meshes[source_idx]['target']['x'].size - 1)

    def get_data_ind_splits(self):
        # Use different strategies when the data_split is a fraction rather than integers
        if type(self.data_split.train) is float:
            # Maximum index is the total number of training examples from all data sources
            maxIndex = len(self)
            # Range creates a list starting at 0 and ending at maxIndex-1 of indices
            data_inds_all = list(range(maxIndex))
            # Here that list gets randomly permutated
            data_inds_all_rand = np.random.permutation(data_inds_all)
            # Training, validation and test indices comes from the data_split fraction making sure that none of the training data is ignored
            # These indices are randomized
            train_idx_max = int(self.data_split.train*maxIndex)
            data_train_inds = list(data_inds_all_rand[0:train_idx_max])
            validation_idx_max = train_idx_max + int(self.data_split.validation * maxIndex)
            data_validation_inds = list(data_inds_all_rand[train_idx_max:validation_idx_max])
            test_idx_max = validation_idx_max + int(self.data_split.test * maxIndex)
            data_test_inds = list(data_inds_all_rand[validation_idx_max:test_idx_max])
            # Sometimes one index is left behind from the rounding effect
            all_inds_set = set(data_inds_all)
            combined_inds_set = set(data_train_inds + data_validation_inds + data_test_inds)
            index_left = list(all_inds_set - combined_inds_set)
            if len(index_left) == 1:
                # Add the index left behind to the training split
                data_train_inds = data_train_inds+index_left
            elif len(index_left) > 1:
                raise Exception("more than one index left behind in splitting the data into train, val and test")
            assert len(self) == len(set(data_train_inds + data_validation_inds + data_test_inds)), 'Length check for the splits failed'

        else:
            data_train_inds = []
            for i, id in enumerate(self.data_split.train):
                idx = self.datasource_id_to_idx(id)
                data_train_inds += list(range(self.data_inds_min[idx], self.data_inds_max[idx]+1))

            data_validation_inds = []
            for i, id in enumerate(self.data_split.validation):
                idx = self.datasource_id_to_idx(id)
                data_validation_inds += list(range(self.data_inds_min[idx], self.data_inds_max[idx] + 1))

            data_test_inds = []
            for i, id in enumerate(self.data_split.test):
                idx = self.datasource_id_to_idx(id)
                data_test_inds += list(range(self.data_inds_min[idx], self.data_inds_max[idx] + 1))

        self.data_train_inds = data_train_inds
        self.data_validation_inds = data_validation_inds
        self.data_test_inds = data_test_inds

    def get_ordered_sample(self, sample_idx, normalize=None):

        """ Retrieves a pair of input and target tensors from all available training cubes based on the global linear
        sample_idx"""
        # Flag to determine whether the input and output are the same. When the same, the transforms are only applied to the input and the data is copied to output.
        inputSameAsOutput = False
        if normalize is None:
            normalize = self.normalize

        # Inputs
        source_idx, bbox_input = self.get_bbox_for_sample_idx(sample_idx, sample_type='input')
        if self.cache_RAM | self.cache_HDD:
            input_ = self.wkw_read_cached(source_idx, 'input', bbox_input)
        else:
            input_ = self.wkw_read(self.data_sources[source_idx].input_path, bbox_input)

        if normalize:
            input_ = WkwData.normalize(input_, self.data_sources[source_idx].input_mean,
                                    self.data_sources[source_idx].input_std)

        input_ = torch.from_numpy(input_).float()
        # squeeze out the depth dimension if a singleton
        if self.input_shape[2] == 1 and input_.dim() > 3 and input_.shape[3] == 1:
            input_ = input_.squeeze(3)
        # Do the transforms only for training data
        if self.transforms and sample_idx in self.data_train_inds:
            input_ = self.transforms(input_)

        # Targets
        source_idx, bbox_target = self.get_bbox_for_sample_idx(sample_idx, sample_type='target')
        if self.data_sources[source_idx].target_binary == 1:
            target = np.asarray(self.data_sources[source_idx].target_class)
        else:
            if (self.data_sources[source_idx].input_path == self.data_sources[source_idx].target_path) & \
                    (bbox_input == bbox_target):
                # targets get converted to torch below
                target = np.asarray(input_)
                inputSameAsOutput = True
            else:
                if self.cache_RAM | self.cache_HDD:
                    target = self.wkw_read_cached(source_idx, 'target', bbox_target)
                else:
                    target = self.wkw_read(self.data_sources[source_idx].target_path, bbox_target)

        if self.pad_target is True:
            target = self.pad(target)

        if self.data_sources[source_idx].target_binary == 1:
            target = torch.from_numpy(target).long()
        else:
            target = torch.from_numpy(target).float()
            # Note(AK): The input gets squeezed above and if the input and output are the same
            # then there's no third dimension to squeeze.
            if self.output_shape[2] == 1 and target.dim() > 3 and target.shape[3] == 1:
                target = target.squeeze(3)
            if self.transforms and not inputSameAsOutput:
                target = self.transforms(target)

        return {'input': input_, 'target': target, 'sample_idx': sample_idx}

    def write_output_to_cache(self,
                              outputs: List[np.ndarray],
                              sample_inds: List[int],
                              output_label: str):

        if type(sample_inds) is torch.Tensor:
            sample_inds = sample_inds.data.numpy().tolist()

        for output_idx, sample_idx in enumerate(sample_inds):
            source_idx, bbox = self.get_bbox_for_sample_idx(sample_idx, 'target')

            wkw_path = self.data_sources[source_idx].input_path
            wkw_bbox = self.data_sources[source_idx].input_bbox

            if wkw_path not in self.data_cache_output:
                self.data_cache_output[wkw_path] = {}

            if str(wkw_bbox) not in self.data_cache_output[wkw_path]:
                self.data_cache_output[wkw_path][str(wkw_bbox)] = {}

            if output_label not in self.data_cache_output[wkw_path][str(wkw_bbox)]:
                data = np.full(wkw_bbox[3:6], np.nan, dtype=np.float32)
                self.data_cache_output[wkw_path][str(wkw_bbox)][output_label] = data

            data_min = np.asarray(bbox[0:3]) - np.asarray(wkw_bbox[0:3])
            data_max = data_min + np.asarray(bbox[3:6])

            data = self.data_cache_output[wkw_path][str(wkw_bbox)][output_label]
            data[data_min[0]:data_max[0], data_min[1]:data_max[1], data_min[2]:data_max[2]] = outputs[output_idx].reshape(self.output_shape)
            self.data_cache_output[wkw_path][str(wkw_bbox)][output_label] = data

    def interpolate_sparse_cache(self, output_label, method=None):
        for wkw_path in self.data_cache_output.keys():
            for wkw_bbox in self.data_cache_output[wkw_path].keys():
                cache = self.data_cache_output[wkw_path][wkw_bbox][output_label]
                for z in range(cache.shape[2]):
                    data = cache[:, :, z]
                    points = np.argwhere(~np.isnan(data))
                    values = data[points[:, 0], points[:, 1]]
                    grid_x, grid_y = np.mgrid[0:data.shape[0], 0:data.shape[1]]
                    data_dense = griddata(points, values, (grid_x, grid_y), method=method)
                    cache[:, :, z] = data_dense

    def get_random_sample(self):
        """ Retrieves a random pair of input and target tensors from all available training cubes"""

        sample_idx = random.sample(range(self.data_inds_max[-1]), 1)

        return self.get_ordered_sample(sample_idx)

    def pad(self, target):
        pad_shape = np.floor((np.asarray(self.input_shape) - np.asarray(self.output_shape)) / 2).astype(int)
        target = np.pad(target,
                        ((pad_shape[0], pad_shape[0]), (pad_shape[1], pad_shape[1]), (pad_shape[2], pad_shape[2])),
                        'constant')
        return target

    def fill_caches(self):
        for data_source_idx, data_source in enumerate(self.data_sources):
            print('Filling caches ... data source {}/{} input'.format(data_source_idx+1, len(self.data_sources)))
            self.fill_cache(data_source.input_path, data_source.input_bbox)
            print('Filling caches ... data source {}/{} target'.format(data_source_idx+1, len(self.data_sources)))
            self.fill_cache(data_source.target_path, data_source.target_bbox)

    def fill_cache(self, wkw_path, wkw_bbox):

        wkw_cache_path = os.path.join(self.cache_HDD_root, wkw_path[1::])
        # Attempt to read from HDD cache if already exists AND the user has requested the HDD cache:
        # Note: This might result in data incompleteness in the cache if another part of the data is already cached
        if self.cache_HDD and os.path.exists(os.path.join(wkw_cache_path, 'header.wkw')):
            data = self.wkw_read(wkw_cache_path, wkw_bbox)
            # If data incomplete read again from remote source
            if self.assert_data_completeness(data) is False:
                data = self.wkw_read(wkw_path, wkw_bbox)
        else:
            data = self.wkw_read(wkw_path, wkw_bbox)

        # If cache to RAM is true, save to RAM
        if self.cache_RAM:
            if wkw_path not in self.data_cache_input:
                self.data_cache_input[wkw_path] = {str(wkw_bbox): data}
            else:
                self.data_cache_input[wkw_path][str(wkw_bbox)] = data

        # If cache to HDD is true, save to HDD
        if self.cache_HDD:
            if not os.path.exists(wkw_cache_path):
                os.makedirs(wkw_cache_path)

            if not os.path.exists(os.path.join(wkw_cache_path, 'header.wkw')):
                self.wkw_create(wkw_cache_path, wkw_dtype=self.wkw_header(wkw_path).voxel_type)

            self.wkw_write(wkw_cache_path, wkw_bbox, data)

    def wkw_read_cached(self, source_idx, source_type, wkw_bbox):

        key = source_type+'_path'
        key_idx = self.data_sources[source_idx]._fields.index(key)
        wkw_path = self.data_sources[source_idx][key_idx]

        key = source_type + '_bbox'
        key_idx = self.data_sources[source_idx]._fields.index(key)
        abs_pos = self.data_sources[source_idx][key_idx]

        # Attempt to load bbox from RAM cache
        if (wkw_path in self.data_cache_input) & (str(abs_pos) in self.data_cache_input[wkw_path]):

            rel_pos = np.asarray(wkw_bbox[0:3]) - np.asarray(abs_pos[0:3])
            data = self.data_cache_input[wkw_path][str(abs_pos)][
                :,
                rel_pos[0]:rel_pos[0] + wkw_bbox[3],
                rel_pos[1]:rel_pos[1] + wkw_bbox[4],
                rel_pos[2]:rel_pos[2] + wkw_bbox[5],
                ]

        # Attempt to load bbox from HDD cache
        else:
            wkw_cache_path = os.path.join(self.cache_HDD_root, wkw_path[1::])
            if os.path.exists(os.path.join(wkw_cache_path, 'header.wkw')):
                data = self.wkw_read(wkw_cache_path, wkw_bbox)
            # If data incomplete, load conventionally
            else:
                data = self.wkw_read(wkw_path, wkw_bbox)

        return data

    def wkw_write_cache(self,
                        output_label,
                        output_wkw_root,
                        wkw_path=None,
                        wkw_bbox=None,
                        output_dtype=None,
                        output_dtype_fn=None,
                        output_block_type=1):

        if output_dtype is None:
            output_dtype = np.uint8

        if output_dtype_fn is None:
            output_dtype_fn = lambda x: x

        for path in self.data_cache_output.keys():
            if (wkw_path is not None) and (wkw_path != path):
                continue

            _, wkw_mag = os.path.split(path)
            output_wkw_path = os.path.join(output_wkw_root, output_label, wkw_mag)
            if not os.path.exists(output_wkw_path):
                os.makedirs(output_wkw_path)
                self.wkw_create(output_wkw_path, output_dtype, output_block_type)

            for bbox in self.data_cache_output[path].keys():
                if (wkw_bbox is not None) and (wkw_bbox != bbox):
                    continue

                data = np.expand_dims(output_dtype_fn(self.data_cache_output[path][bbox][output_label])
                                      .astype(output_dtype), axis=0)
                print('Writing cache to wkw ... ' + output_wkw_path + ' | ' + bbox)
                bbox_from_str = [int(x) for x in bbox[1:-1].split(',')]
                self.wkw_write(output_wkw_path, bbox_from_str, data)

    def get_bbox_for_sample_idx(self, sample_idx, sample_type='input'):
        source_idx, mesh_inds = self.get_source_mesh_for_sample_idx(sample_idx)
        if sample_type == 'input':
            shape = self.input_shape
        else:
            shape = self.output_shape
        origin = [
            int(self.data_meshes[source_idx][sample_type]['x'][mesh_inds[0], mesh_inds[1], mesh_inds[2]] - np.floor(shape[0] / 2)),
            int(self.data_meshes[source_idx][sample_type]['y'][mesh_inds[0], mesh_inds[1], mesh_inds[2]] - np.floor(shape[1] / 2)),
            int(self.data_meshes[source_idx][sample_type]['z'][mesh_inds[0], mesh_inds[1], mesh_inds[2]] - np.floor(shape[2] / 2)),
        ]
        bbox = origin + list(shape)

        return source_idx, bbox

    def get_center_for_sample_idx(self, sample_idx: int, sample_type: str = 'input'):
        """Get the coordinate of the center(mesh) of the sample given by sample idx"""
        source_idx, mesh_inds = self.get_source_mesh_for_sample_idx(sample_idx)
        this_mesh = self.data_meshes[source_idx][sample_type]
        center = [this_mesh[dim][mesh_inds[0], mesh_inds[1], mesh_inds[2]] for dim in['x', 'y', 'z']]
        return center
    
    def get_source_mesh_for_sample_idx(self, sample_idx):
        # Get appropriate training data cube sample_idx based on global linear sample_idx
        source_idx = int(np.argmax(np.asarray(self.data_inds_max) >= int(sample_idx)))
        # Get appropriate subscript index for the respective training data cube, given the global linear index
        mesh_inds = np.unravel_index(sample_idx - self.data_inds_min[source_idx],
                                     dims=self.data_meshes[source_idx]['target']['x'].shape)

        return source_idx, mesh_inds

    @lru_cache(maxsize=128)
    def get_source_idx_from_sample_idx(self, sample_idx):
        """Get the [data]source index from the linear index of the sample"""
        source_idx = int(np.argmax(np.asarray(self.data_inds_max) >= int(sample_idx)))
        return source_idx

    @lru_cache(maxsize=128)
    def get_target_from_sample_idx(self, sample_idx):
        """Get the binary target class from the linear index of the sample"""
        source_idx = self.get_source_idx_from_sample_idx(sample_idx) 
        target_class = self.data_sources[source_idx].target_class
        return target_class 

    def get_datasources_stats(self, num_samples=30):
        return [self.get_datasource_stats(i, num_samples) for i in range(len(self.data_sources))]

    def get_datasource_stats(self, data_source_idx, num_samples=30):
        sample_inds = np.random.random_integers(self.data_inds_min[data_source_idx],
                                                self.data_inds_max[data_source_idx], num_samples)
        means = []
        stds = []
        for i, sample_idx in enumerate(sample_inds):
            print('Getting stats from dataset ... sample {} of {}'.format(i, num_samples))
            data = self.get_ordered_sample(sample_idx, normalize=False)
            means.append(np.mean(data['input'].data.numpy()))
            stds.append(np.std(data['input'].data.numpy()))

        return {'mean': float(np.around(np.mean(means), 1)), 'std': float(np.around(np.mean(stds), 1))}

    def update_datasources_stats(self, num_samples=30):
        [self.update_datasource_stats(i, num_samples) for i in range(len(self.data_sources))]

    def update_datasource_stats(self, data_source_idx, num_samples=30):

        stats = self.get_datasource_stats(data_source_idx, num_samples)
        self.data_sources[data_source_idx] = self.data_sources[data_source_idx]._replace(input_mean=stats['mean'])
        self.data_sources[data_source_idx] = self.data_sources[data_source_idx]._replace(input_std=stats['std'])

    def datasource_id_to_idx(self, id):
        idx = [data_source.id for data_source in self.data_sources].index(id)
        return idx

    def datasource_idx_to_id(self, idx):
        id = self.data_sources[idx].id
        return id

    def show_sample(self, sample_idx, orient_wkw=False):

        data = self.__getitem__(sample_idx)
        input_ = data['input'].data.numpy().squeeze()
        target = data['target'].data.numpy().squeeze()
        if orient_wkw:
            input_ = np.rot90(np.flipud(input_), k=-1)
        fig, axs = plt.subplots(1, 2)
        axs[0].imshow(input_, cmap='gray')
        while target.ndim < 2:
            target = np.expand_dims(target, 0)
        target = np.rot90(np.flipud(target), k=-1)
        axs[1].imshow(target, cmap='gray', vmin=0, vmax=1)
        # AK: Add to pipe the figure through X11
        plt.show()

    @classmethod
    def init_from_config(cls, config: NamedTuple, data_source_list: List = None):
        """
        class method to initialize a dataset from a configuration named tuple
        """
        # if data sources are not given, read them from the json directory
        if data_source_list is None:
            assert config.datasources_json_path is not None
            data_sources = WkwData.datasources_from_json(config.datasources_json_path)
        else:
            data_sources = data_source_list
        # create the wkwdataset
        dataset = cls(
            input_shape=config.input_shape,
            target_shape=config.output_shape,
            data_sources=data_sources,
            cache_RAM=config.cache_RAM,
            cache_HDD=config.cache_HDD,
            cache_HDD_root=config.cache_HDD_root
        )
        return dataset

    @staticmethod
    def config_wkwdata(datasources_json_path: str = None,
                       input_shape: Tuple = (140, 140, 1),
                       output_shape: Tuple = (140, 140, 1),
                       cache_HDD: bool = False,
                       cache_RAM: bool = False,
                       batch_size: int = 256,
                       num_workers: int = 8):
        """ Return a named tuple with the parameters for initialization of a wkwdata"""
        fieldnames = 'input_shape, output_shape, cache_RAM, cache_HDD, batch_size, num_workers, cache_HDD_root, datasources_json_path'
        config = namedtuple('config', fieldnames)
        config.datasources_json_path = datasources_json_path
        config.input_shape = input_shape
        config.output_shape = output_shape
        config.cache_RAM = cache_RAM
        config.cache_HDD = cache_HDD
        config.batch_size = batch_size
        config.num_workers = num_workers
        config.cache_HDD_root = os.path.join(get_data_dir(), '.cache/')
        return config
    
    @staticmethod
    def collate_fn(batch):
        input_ = torch.cat([torch.unsqueeze(item['input'], dim=0) for item in batch], dim=0)
        target = torch.cat([torch.unsqueeze(item['target'], dim=0) for item in batch], dim=0)
        sample_idx = [item['sample_idx'] for item in batch]
        return {'input': input_, 'target': target, 'sample_idx': sample_idx}

    @staticmethod
    def normalize(data, mean, std):
        return (np.asarray(data) - mean) / std

    @staticmethod
    def wkw_header(wkw_path):
        with wkw.Dataset.open(wkw_path) as w:
            header = w.header

        return header

    @staticmethod
    def wkw_read(wkw_path, wkw_bbox):
        with wkw.Dataset.open(wkw_path) as w:
            data = w.read(wkw_bbox[0:3], wkw_bbox[3:6])

        return data

    @staticmethod
    def wkw_write(wkw_path, wkw_bbox, data):
        with wkw.Dataset.open(wkw_path) as w:
            w.write(wkw_bbox[0:3], data)

    @staticmethod
    def wkw_create(wkw_path, wkw_dtype=np.uint8, wkw_block_type=1):
        wkw.Dataset.create(wkw_path, wkw.Header(voxel_type=wkw_dtype, block_type=wkw_block_type))

    @staticmethod
    def assert_data_completeness(data):
        if (np.any(data[:, 0, :, :]) & np.any(data[:, -1, :, :]) & np.any(data[:, :, 0, :]) & np.any(data[:, :, -1, :])
                & np.any(data[:, :, :, 0]) & np.any(data[:, :, :, -1])):
            flag = True
        else:
            flag = False
        return flag

    @staticmethod
    def disk_usage(root):
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(root):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)

        return np.floor(total_size/1024/1024)  # MiB

    @staticmethod
    def datasources_bbox_from_json(json_path, bbox_ext, bbox_idx, datasource_idx=0):
        """ Crops out sub bbox from template json given linear index (parallelize over prediction volumes)"""

        datasource = WkwData.datasources_from_json(json_path)[datasource_idx]
        corner_min = (np.ceil(np.array(datasource.input_bbox[0:3])/np.array(bbox_ext))*np.array(bbox_ext)).astype(int)
        corner_max = (np.floor((np.array(datasource.input_bbox[0:3])+np.array(datasource.input_bbox[3:6]))/
                              np.array(bbox_ext))*np.array(bbox_ext)).astype(int)
        x = np.arange(corner_min[0], corner_max[0], bbox_ext[0])
        y = np.arange(corner_min[1], corner_max[1], bbox_ext[1])
        z = np.arange(corner_min[2], corner_max[2], bbox_ext[2])
        xm, ym, zm = np.meshgrid(x, y, z)
        xi, yi, zi = np.unravel_index(bbox_idx, (len(x), len(y), len(z)))
        bbox = [xm[xi, yi, zi], ym[xi, yi, zi], zm[xi, yi, zi], *bbox_ext]
        datasource = datasource._replace(input_bbox=bbox, target_bbox=bbox)

        return [datasource]

    @staticmethod
    def datasources_from_json(json_path):
        with open(json_path) as f:
            datasources_dict = json.load(f)

        datasources = []
        for key in datasources_dict.keys():
            datasource = DataSource(**datasources_dict[key])
            datasources.append(datasource)

        return datasources

    @staticmethod
    def datasources_to_json(datasources, json_path):

        dumps = '{'
        for datasource_idx, datasource in enumerate(datasources):
            dumps += '\n    "datasource_{}"'.format(datasource.id) + ': {'
            dumps += json.dumps(datasource._asdict(), indent=8)[1:-1]
            dumps += "    },"
        dumps = dumps[:-1] + "\n}"

        with open(json_path, 'w') as f:
            f.write(dumps)

    @staticmethod
    def write_short_ds_json(datasources: Union[dict, list], json_path: str, convert_to_short: bool = False):
        """
        Write a compressed version of the json files with shared properties factored
        Args:
            datasources: data source to write (dictionary or list[DataSource] representation)
            json_path: The path to json file
        Returns: None
        """
        # convert to shortened form if asked
        if convert_to_short:
            datasources = WkwData.convert_to_short_ds(data_sources=datasources)

        if not isinstance(datasources, dict):
            ds_dict = WkwData.convert_ds_to_dict(datasources)
        else:
            ds_dict = datasources
        with open(json_path, 'w') as f:
            json.dump(ds_dict, f, indent=4)

    @staticmethod
    def read_short_ds_json(json_path: str):
        """
        Read a compressed version of the json files
        Args:
            json_path: The path to the json file
        Returns:
            datasources: list of DataSources
        """
        with open(json_path, 'r') as f:
            ds_dict = json.load(f)
        # Add shared properties
        if 'shared_properties' in ds_dict.keys():
            p_key = 'shared_properties'
            # Keys of the data sources
            ds_keys = list(ds_dict.keys())
            ds_keys.remove('shared_properties')
            assert all(['datasource_' in key for key in ds_keys]), 'data source key names problem'
            for key in ds_keys:
                ds_dict[key].update(ds_dict[p_key])
            del ds_dict[p_key]
        # Convert to DataSource list
        datasources = WkwData.convert_ds_to_list(ds_dict)
        return datasources

    @staticmethod
    def convert_to_short_ds(data_sources: Union[list, dict], shared_properties: dict = None) -> dict:  
        """
        Convert to a shortened version of the data sources
        Args:
            data_sources: list of sources with all fields individually given. If given a dict with shared properties, It would do nothing.
            shared_properties: dictionary with the fields shared by all individual data sources.
        Returns:
            short_dict: The compact data source with shared properties separated into a separate field. 
        """
        # if the given source dict has the 'shared_property' field do nothing and return the dict
        if isinstance(data_sources, dict) and 'shared_properties' in data_sources.keys():
            print('data sources already contains the shared_properties. No change is done')
            return data_sources 
        # if not given, make the shared dict
        if shared_properties is None:
            shared_properties = WkwData.ds_find_shared_properties(data_sources=data_sources)
        ds_dict = WkwData.convert_ds_to_dict(data_sources)
        key2remove_list = list(shared_properties.keys())
        # Loop over each data source
        for ds_key in ds_dict:
            # Removal of shared properties
            for key2remove in key2remove_list:
                ds_dict[ds_key].pop(key2remove, None)
        # Concatenate the shared properties
        short_dict = {**{'shared_properties': shared_properties}, **ds_dict}
        return short_dict

    @staticmethod
    def ds_find_shared_properties(data_sources: list) -> dict:
        """
        Find the properties that are shared amongst all the data_sources
        Note: the equality operator(==) is used for the properties
        Args:
            data_sources: a list of data sources
        Returns:
            shared_prop: Shared properties as a dictionary
        """
        # Remove the shared properties from the data sources
        assert isinstance(data_sources, list)
        long_ds_dict = WkwData.convert_ds_to_dict(data_sources)
        # Here try to find the common groups in the json
        # Get data source and per datasource keys
        ds_keys = list(long_ds_dict)
        per_ds_keys = list(long_ds_dict[ds_keys[0]])
        # initialize with empty lists for each key
        list_all = {k: [] for k in per_ds_keys}
        # get a list of all elements to find shared properties amongst all data sources
        for key in ds_keys:
            cur_ds = long_ds_dict[key]
            for per_key in per_ds_keys:
                list_all[per_key].append(cur_ds[per_key])
        # Create the shared property dictionary
        shared_prop = {}
        for key in list_all:
            cur_elem_list = list_all[key]
            cur_elem_iter = iter(cur_elem_list)
            first = next(cur_elem_iter)
            is_identical = all(first == x for x in cur_elem_iter)
            if is_identical:
                shared_prop.update({key: cur_elem_list[0]})
        return shared_prop
    
    @staticmethod
    def compare_ds_targets(two_datasources: List[Dict],
                           source_names: List[str],
                           target_names: List[str] = ['Debris', 'Myelin']) -> List:
        """
        Compare data sources and return the differnce.
        Args:
             two_datasources: A List that contains the two data sources as dictionaries
             source_names: The name of jsons. Used as row names in difference dataframes
             target_names: The names for each of the binary target classes 
        """
        # Read the two jsons

        # Check length equality
        assert len(set([len(j) for j in two_datasources])) == 1

        t_string = 'target_class'
        diff_sources = []
        for index, ((key_1, source_1), (key_2, source_2)) in enumerate(zip(two_datasources[0].items(), two_datasources[1].items())):
            assert key_1 == key_2
            # assert equality of all fields except for the target class
            fields = list(source_1.keys())
            fields.remove(t_string)
            for f in fields:
                assert source_1[f] == source_2[f]
            # If target class is different record the result for each source as a data frame
            if source_1[t_string] != source_2[t_string]:
                cur_diff_dict = dict.fromkeys([key_1])
                cur_diff_df = pd.DataFrame([source_1[t_string], source_2[t_string]],
                                           columns=target_names, index=source_names)
                cur_diff_dict[key_1] = cur_diff_df
                diff_sources.append(cur_diff_dict)
            
        return diff_sources

    @staticmethod
    def convert_ds_to_dict(datasources: list):
        """
        Convert DataSource list to dictionary
        Args:
            datasources: List of DataSources
        Returns:
            Dictionary of data sources
        """
        assert isinstance(datasources, list)
        return {f'datasource_{d.id}': d._asdict() for d in datasources}

    @staticmethod
    def convert_ds_to_list(datasources_dict: dict):
        """
        Convert dictionary of data sources into the list
        Args:
            datasources_dict: The dictioanry of data sources
        Return
            list of DataSource objects
        """
        assert isinstance(datasources_dict, dict)
        return [DataSource(**cur_ds) for cur_ds in datasources_dict.values()] 

    @staticmethod
    def concat_datasources(json_paths_in: Sequence[str], json_path_out: str = None):
        """
        Concatenate multiple .json data sources into one list and possibly write to a new json file
        Args:
            json_paths_in: List of json paths to merge
            json_path_out: [Optional] Path to write the merged json file
        Returns:
            List of merged data sources
        """
        all_ds = []
        for json_path in json_paths_in:
            cur_ds = WkwData.datasources_from_json(json_path)
            # Concatenate the data sources from current json to a list of all data sources
            all_ds = all_ds + cur_ds

        data_sources_out = []
        for it, cur_data_source in enumerate(all_ds):
            # Correct the id of the data source (sequential starting at 0)
            cur_ds_id_corrected = cur_data_source._replace(id=str(it))
            data_sources_out.append(cur_ds_id_corrected)

        # Write json to a output file if name is given
        if json_path_out is not None:
            WkwData.datasources_to_json(data_sources_out, json_path_out)
        # return the list of data sources
        return data_sources_out

    @staticmethod
    def get_common_transforms():
        """
        Get the transform object with the common transforms (Flips, rotation90)
        Args: None
        Returns:
            transformation: the transformation
        """
        common_transforms = transforms.Compose([
            transforms.RandomFlip(p=0.5, flip_plane=(1, 2)),
            transforms.RandomFlip(p=0.5, flip_plane=(2, 1)),
            transforms.RandomRotation90(p=1.0, mult_90=[0, 1, 2, 3], rot_plane=(1, 2))
            ])
        return common_transforms
