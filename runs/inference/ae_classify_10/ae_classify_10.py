import os

import numpy as np
from scipy.special import logit, expit

import torch
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler

from genEM3.data.wkwdata import WkwData, DataSplit
from genEM3.model.autoencoder2d import Encoder_4_sampling_bn_1px_deep_convonly_skip, AE_Encoder_Classifier, Classifier3Layered
from genEM3.inference.inference import Predictor

torch.multiprocessing.set_sharing_strategy('file_system')

run_root = os.path.dirname(os.path.abspath(__file__))
cache_HDD_root = os.path.join(run_root, '.cache/')
datasources_json_path = os.path.join(run_root, 'datasources_predict.json')
state_dict_path = os.path.join(run_root, '../../training/ae_classify_v09_3layer_unfreeze_latent_debris_clean_transform_add_clean2_wiggle/.log/run_w_pr/epoch_700/model_state_dict')
device = 'cpu'

output_wkw_root = '/tmpscratch/webknossos/Connectomics_Department/2018-11-13_scMS109_1to7199_v01_l4_06_24_fixed_mag8_artifact_pred'
output_label = 'probs_sparse'

batch_size = 128
input_shape = (140, 140, 1)
output_shape = (1, 1, 1)
num_workers = 12

kernel_size = 3
stride = 1
n_fmaps = 16
n_latent = 2048
input_size = 140
output_size = input_size
model = AE_Encoder_Classifier(
    Encoder_4_sampling_bn_1px_deep_convonly_skip(input_size, kernel_size, stride, n_latent=n_latent),
    Classifier3Layered(n_latent=n_latent))

datasources = WkwData.datasources_from_json(datasources_json_path)
dataset = WkwData(
    input_shape=input_shape,
    target_shape=output_shape,
    data_sources=datasources,
    stride=(35, 35, 1),
    cache_HDD=False,
    cache_RAM=True,
    cache_HDD_root=cache_HDD_root
)

prediction_loader = torch.utils.data.DataLoader(
    dataset=dataset, batch_size=batch_size, num_workers=num_workers)

checkpoint = torch.load(state_dict_path, map_location=lambda storage, loc: storage)
state_dict = checkpoint['model_state_dict']
model.load_state_dict(state_dict)

output_prob_fn = lambda x: np.exp(x[:, 1, 0, 0])
# output_dtype = np.uint8
output_dtype = np.float32
# output_dtype_fn = lambda x: (logit(x) + 16) * 256 / 32
output_dtype_fn = lambda x: x
# output_dtype_fni = lambda x: expit(x / 256 * 32 - 16)
output_dtype_fni = lambda x: x

predictor = Predictor(
    model=model,
    dataloader=prediction_loader,
    output_prob_fn=output_prob_fn,
    output_dtype_fn=output_dtype_fn,
    output_dtype=output_dtype,
    output_label=output_label,
    output_wkw_root=output_wkw_root,
    output_wkw_compress=False,
    device=device,
    interpolate=None)

predictor.predict()
print('done')



