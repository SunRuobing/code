# %%
import copy
import math
import torch
import torch.nn as nn
import torch.nn.functional as F

import pytorch_lightning as pl
from sunyata.pytorch.arch.base import BaseCfg, set_requires_grad, BYOL_EMA
from sunyata.pytorch.arch.loss import InfoNCE, BarlowTwins, ECELoss, BarlowTwinsLoss3d
from sunyata.pytorch_lightning.base import BaseModule

# %%
from sunyata.pytorch.data.wikitext import (WikiTextDataModule,
                                            shift_one_token)

from sunyata.pytorch.layer.transformer import TransformerCfg, TransformerLayer

from sunyata.pytorch.arch.contrastive_clm import ContrastiveCLMCfg, ContrastiveCLM, ContrastiveCLMCov
# %%
hidden_dim = 256
cfg = ContrastiveCLMCfg(
    vocab_size = 1000,
    seq_len = 256,
    hidden_dim = hidden_dim,
    transformer = TransformerCfg(
        hidden_dim = hidden_dim,
        num_heads = 2,
        expansion= 2*hidden_dim,
        is_softmax=True,
    ),
    ema_tau = 0.99,
    temperature = None,
    lambda_coeff = 5e-3,
    alpha = 0.,
    batch_size = 32,
    num_layers = 4,
    num_epochs = 1,
    learning_rate = 1e-3 # 1e-3  3e-4
)

# %%
wikitext2 = WikiTextDataModule(subset="2", 
                   data_dir=".data/wikitext/", 
                   batch_size=cfg.batch_size,
                   vocab_size=cfg.vocab_size,
                   seq_len=cfg.seq_len,
                   collate_fn=shift_one_token)  # shift_one_token  None
# %%
wikitext2.tokenizer.decode(wikitext2.train_data[0].tolist(), skip_special_tokens=False)
# https://colab.research.google.com/github/huggingface/notebooks/blob/master/examples/tokenizer_training.ipynb
# https://www.thepythoncode.com/article/pretraining-bert-huggingface-transformers-in-python

# %%
input, target = next(iter(wikitext2.train_dataloader()))
input.shape, target.shape

# %%
class LatentAndCLM(BaseModule):
    def __init__(self, cfg:ContrastiveCLMCfg):
        super().__init__(cfg)
        self.save_hyperparameters('cfg')

        self.student = nn.Sequential(
            # nn.Embedding(cfg.vocab_size, cfg.hidden_dim),
            nn.Sequential(*[
                TransformerLayer(cfg.transformer) for _ in range(0)
            ]),
        )

        self.embed = nn.Embedding(cfg.vocab_size, cfg.hidden_dim)
        torch.nn.init.xavier_normal_(self.embed.weight.data)
        self.digup = nn.Linear(cfg.hidden_dim, cfg.vocab_size, bias=False)
        self.digup.weight = self.embed.weight  # do not add .data

        self.teacher = copy.deepcopy(self.student)
        set_requires_grad(self.teacher, False)

        self.layers = nn.Sequential(
            nn.Sequential(*[
                TransformerLayer(cfg.transformer) for _ in range(cfg.num_layers)
            ]),
        )
        self.predictor = nn.BatchNorm1d(cfg.hidden_dim)

    def forward(self, input, target):
        with torch.no_grad():
            target_embedded = self.embed(target)
            target_embedded = self.teacher(target_embedded)
            target_embedded.detach_()

        input_embedded = self.embed(input)
        input_embedded = self.student(input_embedded)
        output_embedded = self.layers(input_embedded)
        output_embedded = self.predictor(output_embedded.permute(0,2,1)).permute(0,2,1)
        return output_embedded, target_embedded

    def _step(self, batch, mode="train"):  # or "val"
        input, target = batch
        output_embedded, target_embedded = self.forward(input, target)
        # target_embedded.detach_()
        # cosine_loss = nn.SmoothL1Loss()(output_embedded, target_embedded)
        # cosine_loss = nn.MSELoss()(output_embedded, target_embedded)
        # cosine_loss = - nn.CosineSimilarity(dim=-1)(output_embedded, target_embedded).mean()
        cosine_loss = 2 - 2 * (output_embedded * target_embedded).sum(dim=(-1,)).mean()
        self.log(mode + "_cosine_loss", cosine_loss)
        return cosine_loss

    def validation_step(self, batch, batch_idx):
        input, target = batch
        output_embedded, target_embedded = self.forward(input, target)
        # logits = output_embedded @ self.embed.weight.T
        logits = self.digup(output_embedded)
        loss = F.cross_entropy(logits.permute(0, 2, 1), target)
        self.log("val_loss", loss, prog_bar=True)
        accuracy = (logits.argmax(dim=-1) == target).float().mean()
        self.log("val_accuracy", accuracy, prog_bar=True)

# %%
contrastive_clm = LatentAndCLM(cfg)
contrastive_clm.summarize(max_depth=2)
# %%
csv_logger = pl.loggers.CSVLogger(save_dir="lightning_logs/", 
    name="wikitext_2") # , version=2
trainer = pl.Trainer(gpus=1, 
                     max_epochs=cfg.num_epochs, 
                     enable_checkpointing=True,
                    #  callbacks=[BYOL_EMA("student", "teacher", cfg.ema_tau)],
                    #  limit_train_batches=100,  # 1.0 
                    #  limit_val_batches=10,  # 1.0 
                     log_every_n_steps=50,
                     logger=csv_logger)

# %%
trainer.fit(contrastive_clm, wikitext2)
checkpoint_version = contrastive_clm.logger.version

# %%
class ContrastiveCLMTune(BaseModule):
    def __init__(self, cfg:ContrastiveCLMCfg, checkpoint_path, is_fine_tune:bool=False):
        super().__init__(cfg)
        self.save_hyperparameters('cfg')
        self.contrastive_clm = LatentAndCLM.load_from_checkpoint(checkpoint_path)
        if is_fine_tune:
            set_requires_grad(self.contrastive_clm, False)
        self.digup = nn.Linear(cfg.hidden_dim, cfg.vocab_size, bias=False)
        # torch.nn.init.xavier_normal_(self.digup.weight.data)

        # self.digup.weight.data = self.contrastive_clm.embed.weight.clone().detach()

    def forward(self, input, target):
        # with torch.no_grad():
        output_embedded, target_embedded = self.contrastive_clm.forward(input, target)
        # logits = output_embedded @ self.embed.weight.T
        logits = self.digup(output_embedded)

        # input_embedded = self.contrastive_clm.embed(input)

        # output_embedded = self.contrastive_clm.layers(input_embedded)
        # logits = self.digup(output_embedded)
        return logits

    def _step(self, batch, mode="train"):  # or "val"
        input, target = batch
        logits = self.forward(input, target)
        loss = F.cross_entropy(logits.permute(0, 2, 1), target)
        self.log(mode + "_loss", loss)
        accuracy = (logits.argmax(dim=-1) == target).float().mean()
        self.log(mode + "_accuracy", accuracy, prog_bar=True)
        return loss

# %%
import os
checkpoint_path = os.path.join(f"./lightning_logs/wikitext_2/version_{checkpoint_version}/checkpoints/epoch=0-step=261.ckpt")
latent_clm2 = ContrastiveCLMTune(cfg, checkpoint_path, is_fine_tune=False)
# %%
csv_logger = pl.loggers.CSVLogger(save_dir="lightning_logs/", 
    name="wikitext_2") # , version=2
trainer = pl.Trainer(gpus=1, 
                     max_epochs=cfg.num_epochs, 
                     enable_checkpointing=True,
                    #  callbacks=[BYOL_EMA(cfg.ema_tau)],
                    #  limit_train_batches=100,  # 1.0 
                    #  limit_val_batches=10,  # 1.0 
                     log_every_n_steps=50,
                     logger=csv_logger)

# %%
trainer.fit(latent_clm2, wikitext2)
# checkpoint_version = contrastive_clm.logger.version


# %%
# for i, (input, target) in enumerate(wikitext2.train_dataloader()):
output_embedded, target_embedded = contrastive_clm(input, target)
output_embedded.shape, target_embedded.shape
# %%
torch.std(target_embedded, dim=1), torch.mean(target_embedded, dim=1)
# %%
torch.std(output_embedded, dim=1), torch.mean(output_embedded, dim=1)
# %%
-nn.CosineSimilarity(dim=-1)(output_embedded, target_embedded).mean()
# %%
target_embedded[0,0]
# %%
target_embedded[0,1]

# %%
output_embedded[0,0]
# %%
output_embedded[0,1]
# %%
