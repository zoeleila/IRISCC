import sys
sys.path.append('.')

from pathlib import Path
import os
import torch
import torch.nn as nn
import pytorch_lightning as pl
import pandas as pd
import matplotlib.pyplot as plt
from torchmetrics import MeanSquaredError, MeanAbsoluteError
from iriscc.unet import UNet
from iriscc.loss import MaskedMSELoss

layout = {
    "Check Overfit": {
        "loss": ["Multiline", ["loss/train", "loss/val"]],
    },
}

class IRISCCLightningModule(pl.LightningModule):
    def __init__(self, hparams):
        super().__init__()
        self.model = UNet(in_channels=hparams['in_channels'], out_channels=1, init_features=32).float()
        #self.loss = nn.MSELoss()  
        self.loss = MaskedMSELoss(ignore_value = hparams['fill_value'])
        self.metrics_dict = nn.ModuleDict({
                    "rmse": MeanSquaredError(squared=False),
                    "mae": MeanAbsoluteError()
                })
        self.fill_value = hparams['fill_value']    
        self.learning_rate = hparams['learning_rate']
        self.runs_dir = hparams['runs_dir']
        os.makedirs(self.runs_dir, exist_ok=True)

        self.test_metrics = {}
        self.train_step_outputs = []
        self.val_step_outputs = []
        self.save_hyperparameters()

    def forward(self, x):
        return self.model(x) 

    def on_train_start(self):
        self.logger.experiment.add_custom_scalars(layout)
        self.logger.log_hyperparams({'learning_rate': self.learning_rate})

    def common_step(self, x, y):
        y_hat = self(x)
        loss = torch.sqrt(self.loss(y_hat, y))
        return y_hat, loss

    def training_step(self, batch, batch_idx):
        x, y = batch
        y_hat, loss = self.common_step(x, y)
        self.train_step_outputs.append(loss)
        self.log('train_loss', loss, on_step=False, on_epoch=True, prog_bar=True)
        return loss
    
    def on_train_epoch_end(self):
        epoch_average = torch.stack(self.train_step_outputs).mean()
        self.logger.experiment.add_scalar("loss/train", epoch_average, self.current_epoch)
        self.train_step_outputs.clear()

    def validation_step(self, batch, batch_idx):
        x, y = batch
        y_hat, loss = self.common_step(x, y)
        self.log('val_loss', loss, on_step=False, on_epoch=True, prog_bar=True)
        self.val_step_outputs.append(loss)

        for metric in self.metrics_dict.values():
            metric.update(y_hat, y)

        return loss

    def on_validation_epoch_end(self):
        epoch_average = torch.stack(self.val_step_outputs).mean()
        self.logger.experiment.add_scalar("loss/val", epoch_average, self.current_epoch)
        self.val_step_outputs.clear()
        for metric_name, metric in self.metrics_dict.items():
            self.logger.experiment.add_scalar(metric_name, metric.compute(), self.current_epoch)
            metric.reset()
        
    def test_step(self, batch, batch_idx):
        x, y = batch
        y_hat, loss = self.common_step(x, y)
        self.log("test_loss", loss, on_step=False, on_epoch=True, prog_bar=True)
            
        batch_dict = {"loss": loss}
        for metric_name, metric in self.metrics_dict.items():
            metric.update(y_hat, y)
            batch_dict[metric_name] = metric.compute()
            metric.reset()
        self.test_metrics[batch_idx] = batch_dict

        if batch_idx == 0:
            x, y = batch
            y_hat = self(x)

            fig, ax = plt.subplots()
            y_hat[y_hat == self.fill_value] = torch.nan 
            im = ax.imshow(y_hat[0,0,:,:].cpu().numpy(), aspect='equal', cmap='jet')
            plt.colorbar(im, ax=ax, pad=0.05)
            self.logger.experiment.add_figure('Figure/test_yhat_0', fig)

            fig, ax = plt.subplots()
            y[y == self.fill_value] = torch.nan
            im = ax.imshow(y[0,0,:,:].cpu().numpy(), aspect='equal', cmap='jet')
            plt.colorbar(im, ax=ax, pad=0.05)
            self.logger.experiment.add_figure('Figure/test_y_0', fig)


    def build_metrics_dataframe(self):
        data = []
        first_sample = list(self.test_metrics.keys())[0]
        metrics = list(self.test_metrics[first_sample].keys())
        for name_sample, metrics_dict in self.test_metrics.items():
            data.append([name_sample] + [metrics_dict[m].item() for m in metrics])
        return pd.DataFrame(data, columns=["Name"] + metrics)

    def save_test_metrics_as_csv(self, df):
        path_csv = Path(self.logger.log_dir) / "metrics_test_set.csv"
        df.to_csv(path_csv, index=False)
    
    def on_test_epoch_end(self):
        df = self.build_metrics_dataframe()
        self.save_test_metrics_as_csv(df)
        df = df.drop("Name", axis=1)
    
        
    def configure_optimizers(self):
        print(self.model.parameters())
        return torch.optim.AdamW(self.model.parameters(), lr=self.learning_rate)

