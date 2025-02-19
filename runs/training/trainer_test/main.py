import os
import time
import torch
import numpy as np
from matplotlib import pyplot as plt
from torch.utils.data import DataLoader

from genEM3.data.wkwdata import WkwData
from genEM3.data.normalize import Normalizer
from genEM3.model.autoencoder2d import AE, Encoder_4_sampling_bn, Decoder_4_sampling_bn
from genEM3.training.autoencoder import Trainer

# Parameters
run_root = os.path.dirname(os.path.abspath(__file__))

wkw_root = '/tmpscratch/webknossos/Connectomics_Department/' \
                  '2018-11-13_scMS109_1to7199_v01_l4_06_24_fixed_mag8/color/1'

cache_root = os.path.join(run_root, '.cache/')
datasources_json_path = os.path.join(run_root, 'datasources.json')
data_strata = {'training': [1, 2], 'validate': [3], 'test': []}
input_shape = (302, 302, 1)
output_shape = (302, 302, 1)
norm_mean = 148.0
norm_std = 36.0

# Run
data_sources = WkwData.datasources_from_json(datasources_json_path)

# With Caching (cache filled)
dataset = WkwData(
    data_sources=data_sources,
    data_strata=data_strata,
    input_shape=input_shape,
    target_shape=output_shape,
    norm_mean=norm_mean,
    norm_std=norm_std,
    cache_RAM=True,
    cache_HDD=True,
    cache_HDD_root=cache_root,
)

dataloader = DataLoader(dataset, batch_size=24, shuffle=False, num_workers=0)

input_size = 302
output_size = input_size
valid_size = 17
kernel_size = 3
stride = 1
n_fmaps = 8
n_latent = 5000
model = AE(
    Encoder_4_sampling_bn(input_size, kernel_size, stride, n_fmaps, n_latent),
    Decoder_4_sampling_bn(output_size, kernel_size, stride, n_fmaps, n_latent))
criterion = torch.nn.MSELoss()
optimizer = torch.optim.SGD(model.parameters(), lr=0.025, momentum=0.8)

num_epoch = 10
log_int = 10
device = 'cpu'

trainer = Trainer(run_root,
                  dataloader,
                  model,
                  optimizer,
                  criterion,
                  num_epoch,
                  log_int,
                  device)

trainer.train()
