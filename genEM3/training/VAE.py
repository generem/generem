"""Functions used in the training process of VAE"""
import os
import shutil
import torch
import argparse

from torch.nn import functional as F
from torchvision.utils import make_grid
from tqdm import tqdm

from genEM3.util.image import undo_normalize
import genEM3.util.path as gpath
from torch.utils.tensorboard import SummaryWriter

# factor for numerical stabilization of the loss sum
NUM_FACTOR = 10000


def loss_function(recon_x, x, mu, logvar, weight_KLD):
    """Returns the variational loss which is the sum of reconstruction and KL divergence from prior"""
    img_size_recon = torch.tensor(recon_x.shape[2:4]).prod()
    img_size_input = torch.tensor(x.shape[2:4]).prod()
    # reconstruction loss
    BCE = F.mse_loss(recon_x.view(-1, img_size_recon), x.view(-1, img_size_input), reduction='sum')
    # KL divergence loss between the posterior and prior of latent space
    KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
    # Add a dict of separate reconstruction and KL loss
    lossDetail = {'Recon': BCE, 'KLD': KLD, 'weighted_KLD': weight_KLD*KLD}
    return BCE + weight_KLD*KLD, lossDetail


def loss_function_predict(recon_x, x, mu, logvar, weight_KLD, reduction: str = 'none'):
    """Returns the variational loss which is the sum of reconstruction and KL divergence from prior"""
    img_size_recon = torch.tensor(recon_x.shape[2:4]).prod()
    img_size_input = torch.tensor(x.shape[2:4]).prod()
    # reconstruction loss
    BCE = torch.sum(F.mse_loss(recon_x.view(-1, img_size_recon), x.view(-1, img_size_input), reduction=reduction), dim=1, keepdim=True)
    # KL divergence loss between the posterior and prior of latent space
    KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp(), dim=1, keepdim=True)
    # Add a dict of separate reconstruction and KL loss
    lossDetail = {'Recon': BCE, 'KLD': KLD, 'weighted_KLD': weight_KLD*KLD}
    return BCE + weight_KLD*KLD, lossDetail


def train(epoch: int = None,
          model: torch.nn.Module = None,
          train_loader: torch.utils.data.DataLoader = None,
          optimizer: torch.optim = None,
          args: argparse.Namespace = None,
          device: torch.device = torch.device('cpu')):
    """training loop on a batch of data"""
    model.train()
    train_loss = 0
    detailedLoss = {'Recon': 0.0, 'KLD': 0.0, 'weighted_KLD': 0.0}
    for batch_idx, data in tqdm(enumerate(train_loader), total=len(train_loader), desc='train'):
        data = data['input'].to(device)

        optimizer.zero_grad()
        recon_batch = model(data)

        loss, curDetLoss = loss_function(recon_batch,
                                         data,
                                         model.cur_mu,
                                         model.cur_logvar,
                                         model.weight_KLD)
        train_loss += (loss.item() / NUM_FACTOR)
        # Separate loss
        for key in curDetLoss:
            detailedLoss[key] += (curDetLoss.get(key) / NUM_FACTOR)
        # Backprop
        loss.backward()
        optimizer.step()
    num_data_points = len(train_loader.dataset.data_train_inds)
    train_loss /= num_data_points
    train_loss *= NUM_FACTOR

    for key in detailedLoss:
        detailedLoss[key] /= num_data_points
        detailedLoss[key] *= NUM_FACTOR

    return train_loss, detailedLoss


def test(epoch: int = None,
         model: torch.nn.Module = None,
         test_loader: torch.utils.data.DataLoader = None,
         writer: SummaryWriter = None,
         args: argparse.Namespace = None,
         device: torch.device = torch.device('cpu')):
    """Run inference on a batch of data without"""
    model.eval()
    test_loss = 0
    detailedLoss = {'Recon': 0.0, 'KLD': 0.0, 'weighted_KLD': 0.0}
    with torch.no_grad():
        for batch_idx, data in tqdm(enumerate(test_loader), total=len(test_loader), desc='test'):
            data = data['input'].to(device)
            recon_batch = model(data)
            curLoss, curDetLoss = loss_function(recon_batch, data,
                                                model.cur_mu, model.cur_logvar, model.weight_KLD)
            test_loss += (curLoss.item() / NUM_FACTOR)
            # The separate KL and Reconstruction losses
            for key in curDetLoss:
                detailedLoss[key] += (curDetLoss.get(key) / NUM_FACTOR)
            # Add 8 test images and reconstructions to tensorboard
            if batch_idx == 0:
                n = min(data.size(0), 8)
                # concatenate the input data and associated reconstruction
                comparison = torch.cat([data[:n], recon_batch[:n]]).cpu()
                comparison_uint8 = undo_normalize(comparison, mean=148.0, std=36.0)
                img = make_grid(comparison_uint8)
                writer.add_image('test_reconstruction',
                                 img,
                                 epoch)
        writer.add_histogram('input_last_batch_test', data.cpu().numpy(), global_step=epoch)
        writer.add_histogram('reconstruction_last_batch_test', recon_batch.cpu().numpy(), global_step=epoch)
    # Divide by the length of the dataset and multiply by factor used for numerical stabilization
    num_data_points = len(test_loader.dataset.data_test_inds)
    test_loss /= num_data_points
    test_loss *= NUM_FACTOR

    for key in detailedLoss:
        detailedLoss[key] /= num_data_points
        detailedLoss[key] *= NUM_FACTOR

    return test_loss, detailedLoss


def save_checkpoint(state, is_best, outdir='.log'):
    gpath.mkdir(outdir)
    checkpoint_file = os.path.join(outdir, 'checkpoint.pth')
    best_file = os.path.join(outdir, 'model_best.pth')
    torch.save(state, checkpoint_file)
    if is_best:
        shutil.copyfile(checkpoint_file, best_file)


def generate_dir_prefix(max_weight_kld: float = 1.0, warmup_bool: bool = True):
    """Return the prefix for the directory name of the training run"""
    return f'weightedVAE_{max_weight_kld}_warmup_{warmup_bool}_'


def predict(model: torch.nn.Module = None,
            data_loader: torch.utils.data.DataLoader = None,
            device: torch.device = torch.device('cpu')):
    """Run inference on a dataset"""
    model.eval()
    detailedLoss = {'Recon': [], 'KLD': [], 'weighted_KLD': []}
    latent_params = {'Mu': [], 'logvar': []}
    with torch.no_grad():
        for batch_idx, data in tqdm(enumerate(data_loader), total=len(data_loader), desc='test'):
            data = data['input'].to(device)
            recon_batch = model(data)
            curLoss, curDetLoss = loss_function_predict(recon_batch, data,
                                                        model.cur_mu, model.cur_logvar, model.weight_KLD, reduction='none')
            # save latent distribtion parameters
            latent_params['Mu'].append(model.cur_mu)
            latent_params['logvar'].append(model.cur_logvar)
            # The separate KL and Reconstruction losses
            for key in curDetLoss:
                # The squared error losses are summed within each individual 2D example
                detailedLoss[key].append(curDetLoss.get(key))
        # concatenate the result for the different batches
        for key in latent_params:
            latent_params[key] = torch.cat(latent_params[key])
        for key in detailedLoss:
            detailedLoss[key] = torch.cat(detailedLoss[key])
    return {'latent': latent_params, 'loss': detailedLoss}
