import logging
from typing import Optional, Dict, cast

import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer
from torch import nn

from nemo.collections.asr.data.audio_to_text import AudioToCharDataset, TarredAudioToCharDataset
from nemo.collections.asr.losses.ctc import CTCLoss
from nemo.collections.asr.metrics.wer import WER
from nemo.collections.asr.models import ASRModel
from nemo.collections.asr.models.wav2vec.modules.config import Wav2VecDecoderConfig
from nemo.collections.asr.models.wav2vec.wav2vec_model import Wav2VecEncoderModel
from nemo.collections.asr.parts.perturb import process_augmentations
from nemo.core.classes.common import typecheck


class Wav2VecCTCEncoder(nn.Module):
    def __init__(self, wav2vec_encoder: Wav2VecEncoderModel, cfg: Wav2VecDecoderConfig, encoder_dim):
        super().__init__()

        if cfg.mask.apply_mask:
            # Override encoder mask cfg with decoder mask cfg
            self.encoder.mask_cfg = cfg.mask

        self.final_dropout = nn.Dropout(cfg.final_dropout)
        # Add 1 for blank char
        vocab = cfg.vocabulary
        self._num_classes = len(vocab) + 1
        self.apply_mask = cfg.mask.apply_mask
        self.wav2vec_encoder = wav2vec_encoder

        self.proj = self.linear(encoder_dim, self._num_classes)

    def linear(self, in_features, out_features, bias=True):
        m = nn.Linear(in_features, out_features, bias)
        nn.init.xavier_uniform_(m.weight)
        if bias:
            nn.init.constant_(m.bias, 0.0)
        return m

    def forward(self, audio_signal, padding_mask):

        with torch.no_grad():
            x, padding_mask = self.wav2vec_encoder.extract_features(
                source=audio_signal,
                padding_mask=padding_mask,
                mask=self.apply_mask and self.training
            )

        x = self.final_dropout(x)

        if self.proj:
            x = self.proj(x)

        output_lengths = padding_mask.long().sum(-1)
        return x, output_lengths


class Wav2VecASRModel(ASRModel):
    def __init__(self, encoder: Wav2VecEncoderModel, cfg: DictConfig, trainer: Trainer):
        # Get global rank and total number of GPU workers for IterableDataset partitioning, if applicable
        self.global_rank = 0
        self.world_size = 1
        self.local_rank = 0
        if trainer is not None:
            self.global_rank = (trainer.node_rank * trainer.num_gpus) + trainer.local_rank
            self.world_size = trainer.num_nodes * trainer.num_gpus
            self.local_rank = trainer.local_rank

        super().__init__(cfg, trainer)

        schema = OmegaConf.structured(Wav2VecDecoderConfig)
        cfg = cfg.get('params', {})
        if isinstance(cfg, dict):
            cfg = OmegaConf.create(cfg)
        elif not isinstance(cfg, DictConfig):
            raise ValueError(f"cfg was type: {type(cfg)}. Expected either a dict or a DictConfig")
        cfg = OmegaConf.create(OmegaConf.to_container(cfg, resolve=True))
        cfg = OmegaConf.merge(schema, cfg)
        cfg = cast(Wav2VecDecoderConfig, cfg)

        self.encoder = Wav2VecCTCEncoder(
            wav2vec_encoder=encoder,
            cfg=cfg,
            encoder_dim=encoder.encoder_embed_dim
        )

        self.loss = CTCLoss(
            num_classes=self.decoder.num_classes_with_blank - 1,
            zero_infinity=True,
            reduction=self._cfg.get("ctc_reduction", "mean_batch"),
        )

        # Setup metric objects
        self._wer = WER(
            vocabulary=self.decoder.vocabulary,
            batch_dim_index=0,
            use_cer=self._cfg.get('use_cer', False),
            ctc_decode=True,
            dist_sync_on_step=True,
            log_prediction=self._cfg.get("log_prediction", False),
        )

    def _setup_dataloader_from_config(self, config: Optional[Dict]):
        if 'augmentor' in config:
            augmentor = process_augmentations(config['augmentor'])
        else:
            augmentor = None

        shuffle = config['shuffle']

        # Instantiate tarred dataset loader or normal dataset loader
        if config.get('is_tarred', False):
            if ('tarred_audio_filepaths' in config and config['tarred_audio_filepaths'] is None) or (
                    'manifest_filepath' in config and config['manifest_filepath'] is None
            ):
                logging.warning(
                    "Could not load dataset as `manifest_filepath` was None or "
                    f"`tarred_audio_filepaths` is None. Provided config : {config}"
                )
                return None

            shuffle_n = config.get('shuffle_n', 4 * config['batch_size'])
            dataset = TarredAudioToCharDataset(
                audio_tar_filepaths=config['tarred_audio_filepaths'],
                manifest_filepath=config['manifest_filepath'],
                labels=config['labels'],
                sample_rate=config['sample_rate'],
                int_values=config.get('int_values', False),
                augmentor=augmentor,
                shuffle_n=shuffle_n,
                max_duration=config.get('max_duration', None),
                min_duration=config.get('min_duration', None),
                max_utts=config.get('max_utts', 0),
                blank_index=config.get('blank_index', -1),
                unk_index=config.get('unk_index', -1),
                normalize=config.get('normalize_transcripts', False),
                trim=config.get('trim_silence', True),
                parser=config.get('parser', 'en'),
                add_misc=config.get('add_misc', False),
                global_rank=self.global_rank,
                world_size=self.world_size,
                return_pad_mask=True
            )
            shuffle = False
        else:
            if 'manifest_filepath' in config and config['manifest_filepath'] is None:
                logging.warning(f"Could not load dataset as `manifest_filepath` was None. Provided config : {config}")
                return None

            dataset = AudioToCharDataset(
                manifest_filepath=config['manifest_filepath'],
                labels=config['labels'],
                sample_rate=config['sample_rate'],
                int_values=config.get('int_values', False),
                augmentor=augmentor,
                max_duration=config.get('max_duration', None),
                min_duration=config.get('min_duration', None),
                max_utts=config.get('max_utts', 0),
                blank_index=config.get('blank_index', -1),
                unk_index=config.get('unk_index', -1),
                normalize=config.get('normalize_transcripts', False),
                trim=config.get('trim_silence', True),
                load_audio=config.get('load_audio', True),
                parser=config.get('parser', 'en'),
                add_misc=config.get('add_misc', False),
                return_pad_mask=True
            )

        return torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=config['batch_size'],
            collate_fn=dataset.collate_fn,
            drop_last=config.get('drop_last', False),
            shuffle=shuffle,
            num_workers=config.get('num_workers', 0),
            pin_memory=config.get('pin_memory', False),
        )

    @typecheck()
    def forward(self, input_signal, padding_mask):
        log_probs, encoded_len = self.encoder(audio_signal=input_signal, padding_mask=padding_mask)
        greedy_predictions = log_probs.argmax(dim=-1, keepdim=False)
        return log_probs, encoded_len, greedy_predictions

    def model_forward_and_loss(self, batch):
        audio_signal, audio_lengths, transcript, transcript_len, padding_mask = batch
        log_probs, encoded_len, predictions = self.forward(
            input_signal=audio_signal,
            padding_mask=padding_mask
        )

        loss_value = self.loss(
            log_probs=log_probs, targets=transcript, input_lengths=encoded_len, target_lengths=transcript_len
        )
        return loss_value, predictions, transcript, transcript_len

    # PTL-specific methods
    def training_step(self, batch, batch_idx):
        loss_value, predictions, transcript, transcript_len = self.model_forward_and_loss(batch)

        tensorboard_logs = {'train_loss': loss_value, 'learning_rate': self._optimizer.param_groups[0]['lr']}

        if hasattr(self, '_trainer') and self._trainer is not None:
            log_every_n_steps = self._trainer.log_every_n_steps
        else:
            log_every_n_steps = 1

        if (batch_idx + 1) % log_every_n_steps == 0:
            self._wer.update(predictions, transcript, transcript_len)
            wer, _, _ = self._wer.compute()
            tensorboard_logs.update({'training_batch_wer': wer})

        return {'loss': loss_value, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        loss_value, predictions, transcript, transcript_len = self.model_forward_and_loss(batch)
        self._wer.update(predictions, transcript, transcript_len)
        wer, wer_num, wer_denom = self._wer.compute()
        return {
            'val_loss': loss_value,
            'val_wer_num': wer_num,
            'val_wer_denom': wer_denom,
            'val_wer': wer,
        }

    def test_step(self, batch, batch_idx, dataloader_idx=0):
        logs = self.validation_step(batch, batch_idx, dataloader_idx=dataloader_idx)
        test_logs = {
            'test_loss': logs['val_loss'],
            'test_wer_num': logs['val_wer_num'],
            'test_wer_denom': logs['val_wer_denom'],
            'test_wer': logs['val_wer'],
        }
        return test_logs