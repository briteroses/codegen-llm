import collections
import glob
import inspect
import math
import sys
import os
import re
import json
import shutil
import time
import warnings
from pathlib import Path
import importlib.util
from packaging import version
from tqdm import tqdm
from transformers import Trainer
from transformers.modeling_utils import PreTrainedModel
from transformers.training_args import ParallelMode, TrainingArguments
from transformers.utils import logging
from transformers.trainer_utils import (
    PREFIX_CHECKPOINT_DIR,
    BestRun,
    EvalPrediction,
    HPSearchBackend,
    PredictionOutput,
    TrainOutput,
    default_compute_objective,
    default_hp_space,
    set_seed,
    speed_metrics,
)
from transformers.file_utils import (
    WEIGHTS_NAME,
    is_apex_available,
    is_datasets_available,
    is_in_notebook,
    is_torch_tpu_available,
)
from transformers.trainer_callback import (
    CallbackHandler,
    DefaultFlowCallback,
    PrinterCallback,
    ProgressCallback,
    TrainerCallback,
    TrainerControl,
    TrainerState,
)
from transformers.trainer_pt_utils import (
    reissue_pt_warnings,
)

from transformers.utils import logging
from transformers.data.data_collator import DataCollator, DataCollatorWithPadding, default_data_collator
import torch
import torch.nn as nn
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union
from torch.utils.data.dataloader import DataLoader
from torch.utils.data.dataset import Dataset
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data.sampler import RandomSampler, SequentialSampler

if is_torch_tpu_available():
    import torch_xla.core.xla_model as xm
    import torch_xla.debug.metrics as met
    import torch_xla.distributed.parallel_loader as pl

if is_apex_available():
    from apex import amp

if version.parse(torch.__version__) >= version.parse("1.6"):
    _is_native_amp_available = True
    from torch.cuda.amp import autocast

if is_datasets_available():
    import datasets

from transformers.trainer import _model_unwrap
from transformers.optimization import Adafactor, AdamW, get_scheduler
import copy

# Set path to SentEval
PATH_TO_SENTEVAL = './SentEval'
PATH_TO_DATA = './SentEval/data'

# Import SentEval
sys.path.insert(0, PATH_TO_SENTEVAL)
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from simcse.run_inference import config as retrieval_config
from retriever.eval import eval_retrieval_from_file, eval_hit_from_file

logger = logging.get_logger(__name__)


class CLTrainer(Trainer):

    def post_processing(self):

        if self.is_world_process_zero():
            # rename the best model
            best_ep = \
            sorted(self.epoch_metric.items(), key=lambda x: x[1][f"eval_{self.metric_for_best_model}"], reverse=True)[
                0][0]
            src_fd = f"{self.args.output_dir}/{PREFIX_CHECKPOINT_DIR}-{best_ep}"
            tgt_fd = f"{self.args.output_dir}/{PREFIX_CHECKPOINT_DIR}-best"
            shutil.move(src_fd, tgt_fd)
            logger.info(f'rename {src_fd} to {tgt_fd}')
            for fd in os.listdir(self.args.output_dir):
                if 'checkpoint' in fd and '-best' not in fd:
                    shutil.rmtree(f"{self.args.output_dir}/{fd}")
                    logger.info(f'remove {fd}')
            src_r_file = f"{self.tmp_result_folder}/result_file_{best_ep}.json"
            tgt_r_file = f'{tgt_fd}/dev_result.json'
            shutil.copyfile(src_r_file, tgt_r_file)
            logger.info(f"copy the best file from {src_r_file} to {tgt_r_file}")

    def evaluate(self) -> Dict[str, float]:
        """use the same full retrieval pool for validation.
        The function calls run_inference.py to have the exact same validation procedure as the inference."""

        if self.is_world_process_zero():

            # SentEval prepare and batcher
            def prepare(params, samples):
                return

            from run_inference import CodeT5Retriever
            if self.args.eval_form == 'retrieval':
                tmp_folder = f"{self.args.output_dir}/tmp_eval"
                self.tmp_result_folder = tmp_folder
                os.makedirs(tmp_folder, exist_ok=True)

                self.model.eval()
                eval_config_str = [
                    f"--source_embed_save_file {tmp_folder}/src_embedding",
                    f"--target_embed_save_file {tmp_folder}/tgt_embedding",
                    f"--save_file {tmp_folder}/result_file_{self.state.global_step}.json",
                    f"--source_file {self.args.eval_src_file}",
                    f"--target_file {self.args.eval_tgt_file}",
                    '--log_level non',
                    f'--model_name checkpoint-{self.state.global_step}'
                ]
                eval_config_str = " ".join(eval_config_str)
                eval_config = retrieval_config(eval_config_str)
                eval_config.root_folder = self.args.eval_root_folder

                assert self.args.eval_retriever == 't5'
                se = CodeT5Retriever(eval_config)

                if 'recall' in self.args.metric_for_best_model:
                    metric_func = eval_retrieval_from_file
                    flag = 'recall'
                elif 'hit' in self.args.metric_for_best_model:
                    metric_func = eval_hit_from_file
                    flag = 'hit'
                else:
                    raise NotImplementedError(self.args.metric_for_best_model)

                se.prepare_model(self.model, self.tokenizer)
                se.encode_file(eval_config.source_file, eval_config.source_embed_save_file, normalize_embed=False,
                               from_training=True)
                se.encode_file(eval_config.target_file, eval_config.target_embed_save_file, normalize_embed=False,
                               from_training=True)
                se.retrieve(eval_config.source_embed_save_file,
                            eval_config.target_embed_save_file, eval_config.source_idx_file,
                            eval_config.target_idx_file, eval_config.top_k, eval_config.save_file)

                metrics_1 = metric_func(
                    f"{self.args.eval_oracle_file}",
                    eval_config.save_file)

                if flag == 'recall':
                    se.encode_file(eval_config.source_file, eval_config.source_embed_save_file,
                                   normalize_embed=True, from_training=True)
                    se.encode_file(eval_config.target_file, eval_config.target_embed_save_file,
                                   normalize_embed=True, from_training=True)
                    se.retrieve(eval_config.source_embed_save_file,
                                eval_config.target_embed_save_file, eval_config.source_idx_file,
                                eval_config.target_idx_file, eval_config.top_k, eval_config.save_file)

                    metrics_2 = metric_func(
                        f"{eval_config.root_folder}/{self.args.eval_oracle_file}",
                        eval_config.save_file)

                    if metrics_1['recall'][10] > metrics_2['recall'][10]:
                        metrics = metrics_1
                    else:
                        metrics = metrics_2

                    r = metrics['recall'][10]
                    p = metrics['precision'][10]
                    metrics = {"eval_recall@10": r,
                               "eval_precision@10": p,
                               "eval_f1@10": (2 * r * p) / (r + p + 1e-10)}
                else:
                    metrics = {'eval_hit@1': metrics_1['hit'][1],
                               'eval_hit@10': metrics_1['hit'][10]}

            elif self.args.eval_form == 'reranking':
                from data_utils import tok_sentences
                self.model.eval()

                with open(self.args.eval_file, 'r') as f:
                    dataset = []
                    for line in f:
                        dataset.append(json.loads(line.strip()))
                    logger.info(f'size of the eval data: {len(dataset)}')

                def prepare_features(examples):
                    total = len(examples)
                    sentences = [x['text1'] for x in examples] + [x['text2'] for x in examples]
                    features = tok_sentences(self.tokenizer, sentences, has_hard_neg=False, total=total)
                    # convert them to batch
                    batch = [{} for _ in range(total)]
                    for k, v in features.items():
                        for b_idx, vv in enumerate(v):
                            batch[b_idx][k] = vv
                    return batch

                bs = 48
                f1_list = []
                with torch.no_grad():
                    for i in tqdm(range(0, len(dataset), bs)):
                        batch = dataset[i: i + bs]
                        batch = prepare_features(batch)
                        padded_batch = self.data_collator(batch)
                        for k in padded_batch:
                            if isinstance(padded_batch[k], torch.Tensor):
                                padded_batch[k] = padded_batch[k].to(self.args.device)
                        F1 = self.model(**padded_batch, pairwise_similarity=True).detach().cpu().tolist()
                        f1_list += F1

                results = collections.defaultdict(lambda: {'retrieved': [], 'score': []})
                assert len(f1_list) == len(dataset)
                for sample, prob in zip(dataset, f1_list):
                    results[sample['question_id']]['retrieved'].append(sample['function_key'])
                    results[sample['question_id']]['score'].append(prob)

                for k, v in results.items():
                    sorted_idx = np.argsort(v['score'])[::-1]
                    results[k]['score'] = [results[k]['score'][x] for x in sorted_idx]
                    results[k]['retrieved'] = [results[k]['retrieved'][x] for x in sorted_idx]

                root_folder = "/home/shuyanzh/workshop/op_agent"
                tmp_folder = f"{root_folder}/data/para_data/simcse/eval_tmp_store.{self.args.tmp_tag}"
                save_file = f"{tmp_folder}/result_file_{self.state.global_step}.json"
                with open(save_file, 'w+') as f:
                    json.dump(results, f, indent=2)

                metrics = eval_retrieval_from_file(f"{root_folder}/data/conala/nl.cm/cmd_dev.oracle_man.full.json",
                                                   save_file)
                r = metrics['recall'][10]
                p = metrics['precision'][10]
                metrics = {"eval_recall@10": r,
                           "eval_precision@10": p,
                           "eval_f1@10": (2 * r * p) / (r + p + 1e-10)}

            else:
                raise NotImplementedError

            # if eval_senteval_transfer or self.args.eval_transfer:
            #     avg_transfer = 0
            #     for task in ['MR', 'CR', 'SUBJ', 'MPQA', 'SST2', 'TREC', 'MRPC']:
            #         avg_transfer += results[task]['devacc']
            #         metrics['eval_{}'.format(task)] = results[task]['devacc']
            #     avg_transfer /= 7
            #     metrics['eval_avg_transfer'] = avg_transfer

            self.log(metrics)
            self.epoch_metric[self.state.global_step] = metrics
            return metrics

    def train(self, model_path: Optional[str] = None, trial: Union["optuna.Trial", Dict[str, Any]] = None):
        """
        Main training entry point.

        Args:
            model_path (:obj:`str`, `optional`):
                Local path to the model if the model to train has been instantiated from a local path. If present,
                training will resume from the optimizer/scheduler states loaded here.
            trial (:obj:`optuna.Trial` or :obj:`Dict[str, Any]`, `optional`):
                The trial run or the hyperparameter dictionary for hyperparameter search.

        The main difference between ours and Huggingface's original implementation is that we
        also load model_args when reloading best checkpoints for evaluation.
        """
        # This might change the seed so needs to run first.
        self._hp_search_setup(trial)

        # Model re-init
        if self.model_init is not None:
            # Seed must be set before instantiating the model when using model_init.
            set_seed(self.args.seed)

            model = self.call_model_init(trial)
            if not self.is_model_parallel:
                model = model.to(self.args.device)

            self.model = model
            self.model_wrapped = model

            # Reinitializes optimizer and scheduler
            self.optimizer, self.lr_scheduler = None, None

        # Keeping track whether we can can len() on the dataset or not
        train_dataset_is_sized = isinstance(self.train_dataset, collections.abc.Sized)

        # Data loader and number of training steps
        train_dataloader = self.get_train_dataloader()

        # Setting up training control variables:
        # number of training epochs: num_train_epochs
        # number of training steps per epoch: num_update_steps_per_epoch
        # total number of training steps to execute: max_steps
        if train_dataset_is_sized:
            num_update_steps_per_epoch = len(train_dataloader) // self.args.gradient_accumulation_steps
            num_update_steps_per_epoch = max(num_update_steps_per_epoch, 1)
            if self.args.max_steps > 0:
                max_steps = self.args.max_steps
                num_train_epochs = self.args.max_steps // num_update_steps_per_epoch + int(
                    self.args.max_steps % num_update_steps_per_epoch > 0
                )
            else:
                max_steps = math.ceil(self.args.num_train_epochs * num_update_steps_per_epoch)
                num_train_epochs = math.ceil(self.args.num_train_epochs)
        else:
            # see __init__. max_steps is set when the dataset has no __len__
            max_steps = self.args.max_steps
            num_train_epochs = 1
            num_update_steps_per_epoch = max_steps

        if self.args.deepspeed:
            model, optimizer, lr_scheduler = init_deepspeed(self, num_training_steps=max_steps)
            self.model = model.module
            self.model_wrapped = model  # will get further wrapped in DDP
            self.deepspeed = model  # DeepSpeedEngine object
            self.optimizer = optimizer
            self.lr_scheduler = lr_scheduler
        else:
            self.create_optimizer_and_scheduler(num_training_steps=max_steps)

        self.state = TrainerState()
        self.state.is_hyper_param_search = trial is not None

        # Check if saved optimizer or scheduler states exist
        self._load_optimizer_and_scheduler(model_path)

        model = self.model_wrapped

        # Mixed precision training with apex (torch < 1.6)
        if self.use_apex:
            model, self.optimizer = amp.initialize(model, self.optimizer, opt_level=self.args.fp16_opt_level)

        # Multi-gpu training (should be after apex fp16 initialization)
        if self.args.n_gpu > 1:
            model = torch.nn.DataParallel(model)

        # Distributed training (should be after apex fp16 initialization)
        if self.sharded_dpp:
            model = ShardedDDP(model, self.optimizer)
        elif self.args.local_rank != -1:
            model = torch.nn.parallel.DistributedDataParallel(
                model,
                device_ids=[self.args.local_rank],
                output_device=self.args.local_rank,
                find_unused_parameters=(
                    not getattr(model.config, "gradient_checkpointing", False)
                    if isinstance(model, PreTrainedModel)
                    else True
                ),
            )
            # find_unused_parameters breaks checkpointing as per
            # https://github.com/huggingface/transformers/pull/4659#issuecomment-643356021

        # for the rest of this function `model` is the outside model, whether it was wrapped or not
        if model is not self.model:
            self.model_wrapped = model

        # important: at this point:
        # self.model         is the Transformers Model
        # self.model_wrapped is DDP(Transformers Model), DDP(Deepspeed(Transformers Model)), etc.

        # Train!
        if is_torch_tpu_available():
            total_train_batch_size = self.args.train_batch_size * xm.xrt_world_size()
        else:
            total_train_batch_size = (
                    self.args.train_batch_size
                    * self.args.gradient_accumulation_steps
                    * (torch.distributed.get_world_size() if self.args.local_rank != -1 else 1)
            )

        num_examples = (
            self.num_examples(train_dataloader)
            if train_dataset_is_sized
            else total_train_batch_size * self.args.max_steps
        )

        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {num_examples}")
        logger.info(f"  Num Epochs = {num_train_epochs}")
        logger.info(f"  Instantaneous batch size per device = {self.args.per_device_train_batch_size}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_train_batch_size}")
        logger.info(f"  Gradient Accumulation steps = {self.args.gradient_accumulation_steps}")
        logger.info(f"  Total optimization steps = {max_steps}")

        self.state.epoch = 0
        start_time = time.time()
        epochs_trained = 0
        steps_trained_in_current_epoch = 0

        # Check if continuing training from a checkpoint
        if model_path and os.path.isfile(os.path.join(model_path, "trainer_state.json")):
            self.state = TrainerState.load_from_json(os.path.join(model_path, "trainer_state.json"))
            epochs_trained = self.state.global_step // num_update_steps_per_epoch
            if not self.args.ignore_data_skip:
                steps_trained_in_current_epoch = self.state.global_step % (num_update_steps_per_epoch)
                steps_trained_in_current_epoch *= self.args.gradient_accumulation_steps
            else:
                steps_trained_in_current_epoch = 0

            logger.info("  Continuing training from checkpoint, will skip to saved global_step")
            logger.info(f"  Continuing training from epoch {epochs_trained}")
            logger.info(f"  Continuing training from global step {self.state.global_step}")
            if not self.args.ignore_data_skip:
                logger.info(
                    f"  Will skip the first {epochs_trained} epochs then the first {steps_trained_in_current_epoch} "
                    "batches in the first epoch."
                )

        # Update the references
        self.callback_handler.model = self.model
        self.callback_handler.optimizer = self.optimizer
        self.callback_handler.lr_scheduler = self.lr_scheduler
        self.callback_handler.train_dataloader = train_dataloader
        self.state.trial_name = self.hp_name(trial) if self.hp_name is not None else None
        self.state.trial_params = hp_params(trial) if trial is not None else None
        # This should be the same if the state has been saved but in case the training arguments changed, it's safer
        # to set this after the load.
        self.state.max_steps = max_steps
        self.state.num_train_epochs = num_train_epochs
        self.state.is_local_process_zero = self.is_local_process_zero()
        self.state.is_world_process_zero = self.is_world_process_zero()

        # tr_loss is a tensor to avoid synchronization of TPUs through .item()
        tr_loss = torch.tensor(0.0).to(self.args.device)
        # _total_loss_scalar is updated everytime .item() has to be called on tr_loss and stores the sum of all losses
        self._total_loss_scalar = 0.0
        self._globalstep_last_logged = 0
        self._total_flos = self.state.total_flos
        model.zero_grad()

        self.control = self.callback_handler.on_train_begin(self.args, self.state, self.control)

        # Skip the first epochs_trained epochs to get the random state of the dataloader at the right point.
        if not self.args.ignore_data_skip:
            for epoch in range(epochs_trained):
                # We just need to begin an iteration to create the randomization of the sampler.
                for _ in train_dataloader:
                    break

        for epoch in range(epochs_trained, num_train_epochs):
            if isinstance(train_dataloader, DataLoader) and isinstance(train_dataloader.sampler, DistributedSampler):
                train_dataloader.sampler.set_epoch(epoch)
            epoch_iterator = train_dataloader

            # Reset the past mems state at the beginning of each epoch if necessary.
            if self.args.past_index >= 0:
                self._past = None

            steps_in_epoch = len(train_dataloader) if train_dataset_is_sized else self.args.max_steps
            self.control = self.callback_handler.on_epoch_begin(self.args, self.state, self.control)

            assert train_dataset_is_sized, "currently we only support sized dataloader!"

            inputs = None
            last_inputs = None
            for step, inputs in enumerate(epoch_iterator):
                # Skip past any already trained steps if resuming training
                if steps_trained_in_current_epoch > 0:
                    steps_trained_in_current_epoch -= 1
                    continue

                if (step + 1) % self.args.gradient_accumulation_steps == 0:
                    self.control = self.callback_handler.on_step_begin(self.args, self.state, self.control)

                if ((step + 1) % self.args.gradient_accumulation_steps != 0) and self.args.local_rank != -1:
                    # Avoid unnecessary DDP synchronization since there will be no backward pass on this example.
                    with model.no_sync():
                        tr_loss += self.training_step(model, inputs)
                else:
                    tr_loss += self.training_step(model, inputs)
                self._total_flos += self.floating_point_ops(inputs)

                if (step + 1) % self.args.gradient_accumulation_steps == 0 or (
                        # last step in epoch but step is always smaller than gradient_accumulation_steps
                        steps_in_epoch <= self.args.gradient_accumulation_steps
                        and (step + 1) == steps_in_epoch
                ):
                    # Gradient clipping
                    if self.args.max_grad_norm is not None and self.args.max_grad_norm > 0 and not self.deepspeed:
                        # deepspeed does its own clipping

                        if self.use_amp:
                            # AMP: gradients need unscaling
                            self.scaler.unscale_(self.optimizer)

                        if hasattr(self.optimizer, "clip_grad_norm"):
                            # Some optimizers (like the sharded optimizer) have a specific way to do gradient clipping
                            self.optimizer.clip_grad_norm(self.args.max_grad_norm)
                        else:
                            # Revert to normal clipping otherwise, handling Apex or full precision
                            torch.nn.utils.clip_grad_norm_(
                                amp.master_params(self.optimizer) if self.use_apex else model.parameters(),
                                self.args.max_grad_norm,
                            )

                    # Optimizer step
                    if is_torch_tpu_available():
                        xm.optimizer_step(self.optimizer)
                    elif self.use_amp:
                        self.scaler.step(self.optimizer)
                        self.scaler.update()
                    else:
                        self.optimizer.step()

                    self.lr_scheduler.step()

                    model.zero_grad()

                    self.state.global_step += 1
                    self.state.epoch = epoch + (step + 1) / steps_in_epoch
                    self.control = self.callback_handler.on_step_end(self.args, self.state, self.control)
                    self._maybe_log_save_evaluate(tr_loss, model, trial, epoch)

                if self.control.should_epoch_stop or self.control.should_training_stop:
                    break

            self.control = self.callback_handler.on_epoch_end(self.args, self.state, self.control)
            self._maybe_log_save_evaluate(tr_loss, model, trial, epoch)

            if self.args.tpu_metrics_debug or self.args.debug:
                if is_torch_tpu_available():
                    # tpu-comment: Logging debug metrics for PyTorch/XLA (compile, execute times, ops, etc.)
                    xm.master_print(met.metrics_report())
                else:
                    logger.warning(
                        "You enabled PyTorch/XLA debug metrics but you don't have a TPU "
                        "configured. Check your training configuration if this is unexpected."
                    )
            if self.control.should_training_stop:
                break

        if self.args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of training
            delattr(self, "_past")

        logger.info("\n\nTraining completed. Do not forget to share your model on huggingface.co/models =)\n\n")
        self.post_processing()

        # if self.args.load_best_model_at_end and self.state.best_model_checkpoint is not None:
        #     logger.info(
        #         f"Loading best model from {self.state.best_model_checkpoint} (score: {self.state.best_metric})."
        #     )
        #     if isinstance(self.model, PreTrainedModel):
        #         self.model = self.model.from_pretrained(self.state.best_model_checkpoint, model_args=self.model_args)
        #         if not self.is_model_parallel:
        #             self.model = self.model.to(self.args.device)
        #     else:
        #         state_dict = torch.load(os.path.join(self.state.best_model_checkpoint, WEIGHTS_NAME))
        #         self.model.load_state_dict(state_dict)
        #
        #     if self.deepspeed:
        #         self.deepspeed.load_checkpoint(
        #             self.state.best_model_checkpoint, load_optimizer_states=False, load_lr_scheduler_states=False
        #         )

        metrics = speed_metrics("train", start_time, self.state.max_steps)
        if self._total_flos is not None:
            self.store_flos()
            metrics["total_flos"] = self.state.total_flos
        self.log(metrics)
        self.control = self.callback_handler.on_train_end(self.args, self.state, self.control)
        # add remaining tr_loss
        self._total_loss_scalar += tr_loss.item()

        return TrainOutput(self.state.global_step, self._total_loss_scalar / self.state.global_step, metrics)