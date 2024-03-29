"""Training engine for the amortized inference reconstruction drgnai/v4 model.

This module contains the model training engine and the corresponding configuration
definitions for the amortized inference approach to particle reconstruction originally
introduced by Alex Levy in the drgnai package.

"""
import os
import pickle
from collections import OrderedDict
import numpy as np
from dataclasses import dataclass
from typing import Any
import time

import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

import cryodrgn.utils
from cryodrgn import ctf
from cryodrgn.mrc import MRCFile
from cryodrgn.dataset import make_dataloader
from cryodrgn.trainers import summary
from cryodrgn.models.losses import kl_divergence_conf, l1_regularizer, l2_frequency_bias
from cryodrgn.models.amortized_inference import DRGNai, MyDataParallel
from cryodrgn.masking import CircularMask, FrequencyMarchingMask
from cryodrgn.trainers._base import ModelTrainer, ModelConfigurations


@dataclass
class AmortizedInferenceConfigurations(ModelConfigurations):

    trainer_cls = "AmortizedInferenceTrainer"

    # a parameter belongs to this configuration set if and only if it has a default
    # value defined here, note that children classes inherit these from parents
    model: str = "amort"

    # scheduling
    n_imgs_pose_search: int = 500000
    epochs_sgd: int = 100
    pose_only_phase: int = 0
    # data loading
    batch_size_known_poses: int = 32
    batch_size_hps: int = 8
    batch_size_sgd: int = 256
    # optimizers
    pose_table_optim_type: str = "adam"
    conf_table_optim_type: str = "adam"
    conf_encoder_optim_type: str = "adam"
    lr_pose_table: float = 1.0e-3
    lr_conf_table: float = 1.0e-2
    lr_conf_encoder: float = 1.0e-4
    # masking
    output_mask: str = "circ"
    add_one_frequency_every: int = 100000
    n_frequencies_per_epoch: int = 10
    max_freq: int = None
    l_start_fm: int = 1
    # loss
    beta_conf: float = 0.0
    trans_l1_regularizer: float = 0.0
    l2_smoothness_regularizer: float = 0.0
    # conformations
    variational_het: bool = False
    std_z_init: float = 0.1
    use_conf_encoder: bool = False
    depth_cnn: int = 5
    channels_cnn: int = 32
    kernel_size_cnn: int = 3
    resolution_encoder: str = None
    initial_conf: str = None
    pe_type_conf: str = None
    # hypervolume
    volume_domain: str = "hartley"
    explicit_volume: bool = False
    # pre-training
    pretrain_with_gt_poses: bool = False
    # pose search
    n_iter: int = 4
    n_tilts_pose_search: int = 11
    average_over_tilts: bool = False
    no_trans_search_at_pose_search: bool = False
    n_kept_poses: int = 8
    # others
    palette_type: str = None

    quick_configs = OrderedDict(
        {
            "capture_setup": {
                "spa": dict(),
                "et": {
                    "subtomo_averaging": True,
                    "shuffler_size": 0,
                    "num_workers": 0,
                    "t_extent": 0.0,
                    "batch_size_known_poses": 8,
                    "batch_size_sgd": 32,
                    "n_imgs_pose_search": 150000,
                    "pose_only_phase": 50000,
                    "lr_pose_table": 1.0e-5,
                },
            },
            "reconstruction_type": {"homo": {"z_dim": 0}, "het": dict()},
            "pose_estimation": {
                "abinit": dict(),
                "refine": {"refine_gt_poses": True, "pose_learning_rate": 1.0e-4},
                "fixed": {"use_gt_poses": True},
            },
            "conf_estimation": {
                None: dict(),
                "autodecoder": dict(),
                "refine": dict(),
                "encoder": {"use_conf_encoder": True},
            },
        }
    )

    def __post_init__(self) -> None:
        super().__post_init__()

        if self.model != "amort":
            raise ValueError(
                f"Mismatched model {self.model} for AmortizedInferenceTrainer!"
            )

        if self.batch_size_sgd is None:
            self.batch_size_sgd = self.batch_size
        if self.batch_size_known_poses is None:
            self.batch_size_known_poses = self.batch_size
        if self.batch_size_sgd is None:
            self.batch_size_sgd = self.batch_size

        if self.explicit_volume and self.z_dim >= 1:
            raise ValueError(
                "Explicit volumes do not support heterogeneous reconstruction."
            )

        if self.dataset is None:
            if self.particles is None:
                raise ValueError("Dataset wasn't specified: please specify particles!")
            if self.ctf is None:
                raise ValueError("Dataset wasn't specified: please specify ctf!")

        if self.volume_optim_type not in {"adam"}:
            raise ValueError(
                f"Invalid value `{self.volume_optim_type=}` "
                f"for hypervolume optimizer type!"
            )

        if self.pose_table_optim_type not in {"adam", "lbfgs"}:
            raise ValueError(
                f"Invalid value `{self.pose_table_optim_type=}` "
                f"for pose table optimizer type!"
            )

        if self.conf_table_optim_type not in {"adam", "lbfgs"}:
            raise ValueError(
                f"Invalid value `{self.conf_table_optim_type=}` "
                f"for conformation table optimizer type!"
            )

        if self.conf_encoder_optim_type not in {"adam"}:
            raise ValueError(
                f"Invalid value `{self.conf_encoder_optim_type}` "
                "for conformation encoder optimizer type!"
            )

        if self.output_mask not in {"circ", "frequency_marching"}:
            raise ValueError(f"Invalid value {self.output_mask} for output_mask!")

        if self.pe_type not in {"gaussian"}:
            raise ValueError(f"Invalid value {self.pe_type} for pe_type!")

        if self.pe_type_conf not in {None, "geom"}:
            raise ValueError(f"Invalid value {self.pe_type_conf} for pe_type_conf!")

        if self.volume_domain not in {"hartley"}:
            raise ValueError(
                f"Invalid value {self.volume_domain} for hypervolume_domain."
            )

        if self.n_imgs_pose_search < 0:
            raise ValueError("n_imgs_pose_search must be greater than 0!")

        if self.use_conf_encoder and self.initial_conf:
            raise ValueError(
                "Conformations cannot be initialized when also using an encoder!"
            )

        if self.use_gt_trans and self.pose is None:
            raise ValueError(
                "Poses must be specified to use ground-truth translations!"
            )
        if self.refine_gt_poses:
            self.n_imgs_pose_search = 0
            if self.pose is None:
                raise ValueError("Initial poses must be specified to be refined!")

        if self.subtomo_averaging:
            # TODO: Implement conformation encoder for subtomogram averaging.
            if self.use_conf_encoder:
                raise ValueError(
                    "Conformation encoder is not implemented "
                    "for subtomogram averaging!"
                )

            # TODO: Implement translation search for subtomogram averaging.
            if not (self.use_gt_poses and (self.use_gt_trans or self.t_extent == 0.0)):
                raise ValueError(
                    "Translation search is not implemented for subtomogram averaging!"
                )

        if self.average_over_tilts and self.n_tilts_pose_search % 2 == 0:
            raise ValueError(
                "`n_tilts_pose_search` must be odd to use `average_over_tilts`!"
            )

        if self.n_tilts_pose_search > self.n_tilts:
            raise ValueError("`n_tilts_pose_search` must be smaller than `n_tilts`!")

        if self.use_gt_poses:
            # "poses" include translations
            self.use_gt_trans = True
            if self.pose is None:
                raise ValueError("Ground truth poses must be specified!")

        if self.no_trans:
            self.t_extent = 0.0
        if self.t_extent == 0.0:
            self.t_n_grid = 1


class AmortizedInferenceTrainer(ModelTrainer):
    """An engine for training the reconstruction model on particle data.

    Attributes
    ----------
    configs (TrainingConfigurations):   Values of all parameters that can be
                                        set by the user.

    particle_count (int):  The number of picked particles in the data.
    pretraining (bool):     Whether we are in the pretraining stage.
    epoch (int):    Which training epoch the model is in.

    logger (logging.Logger):    Utility for printing and writing information
                                about the model as it is running.
    """

    # placeholders for runtimes
    run_phases = [
        "dataloading",
        "to_gpu",
        "ctf",
        "encoder",
        "decoder",
        "decoder_coords",
        "decoder_query",
        "loss",
        "backward",
        "to_cpu",
    ]

    configs: AmortizedInferenceConfigurations
    config_cls = AmortizedInferenceConfigurations
    model_lbl = "amort"

    def make_output_mask(self) -> CircularMask:
        if self.configs.output_mask == "circ":
            radius = self.configs.max_freq or self.lattice.D // 2
            output_mask = CircularMask(
                radius,
                self.lattice.coords,
                self.lattice.D,
                self.lattice.extent,
                self.lattice.ignore_DC,
            )

        elif self.configs.output_mask == "frequency_marching":
            output_mask = FrequencyMarchingMask(
                self.lattice,
                self.lattice.D // 2,
                radius=self.configs.l_start_fm,
                add_one_every=self.configs.add_one_frequency_every,
            )

        else:
            raise NotImplementedError

        return output_mask

    def make_volume_model(self) -> nn.Module:
        output_mask = self.make_output_mask()

        # cnn
        cnn_params = {
            "conf": self.configs.use_conf_encoder,
            "depth_cnn": self.configs.depth_cnn,
            "channels_cnn": self.configs.channels_cnn,
            "kernel_size_cnn": self.configs.kernel_size_cnn,
        }

        # conformational encoder
        if self.configs.z_dim > 0:
            self.logger.info(
                "Heterogeneous reconstruction with " f"z_dim = {self.configs.z_dim}"
            )
        else:
            self.logger.info("Homogeneous reconstruction")

        conf_regressor_params = {
            "z_dim": self.configs.z_dim,
            "std_z_init": self.configs.std_z_init,
            "variational": self.configs.variational_het,
        }

        # hypervolume
        hyper_volume_params = {
            "explicit_volume": self.configs.explicit_volume,
            "n_layers": self.configs.hidden_layers,
            "hidden_dim": self.configs.hidden_dim,
            "pe_type": self.configs.pe_type,
            "pe_dim": self.configs.pe_dim,
            "feat_sigma": self.configs.feat_sigma,
            "domain": self.configs.volume_domain,
            "extent": self.lattice.extent,
            "pe_type_conf": self.configs.pe_type_conf,
        }

        # pose search
        if self.epochs_pose_search > 0:
            ps_params = {
                "l_min": self.configs.l_start,
                "l_max": self.configs.l_end,
                "t_extent": self.configs.t_extent,
                "t_n_grid": self.configs.t_ngrid,
                "niter": self.configs.n_iter,
                "nkeptposes": self.configs.n_kept_poses,
                "base_healpy": self.configs.base_healpy,
                "t_xshift": self.configs.t_xshift,
                "t_yshift": self.configs.t_yshift,
                "no_trans_search_at_pose_search": self.configs.no_trans_search_at_pose_search,
                "n_tilts_pose_search": self.configs.n_tilts_pose_search,
                "tilting_func": (
                    self.data.get_tilting_func()
                    if self.configs.subtomo_averaging
                    else None
                ),
                "average_over_tilts": self.configs.average_over_tilts,
            }
        else:
            ps_params = None

        return DRGNai(
            self.lattice,
            output_mask,
            self.particle_count,
            self.image_count,
            cnn_params,
            conf_regressor_params,
            hyper_volume_params,
            resolution_encoder=self.configs.resolution_encoder,
            no_trans=self.configs.no_trans,
            use_gt_poses=self.configs.use_gt_poses,
            use_gt_trans=self.configs.use_gt_trans,
            will_use_point_estimates=self.configs.epochs_sgd >= 1,
            ps_params=ps_params,
            verbose_time=self.configs.verbose_time,
            pretrain_with_gt_poses=self.configs.pretrain_with_gt_poses,
            n_tilts_pose_search=self.configs.n_tilts_pose_search,
        )

    @property
    def epochs_pose_search(self) -> int:
        if self.configs.n_imgs_pose_search > 0:
            epochs_pose_search = max(
                2, self.configs.n_imgs_pose_search // self.particle_count + 1
            )
        else:
            epochs_pose_search = 0

        return epochs_pose_search

    def __init__(self, configs: dict[str, Any]) -> None:
        super().__init__(configs)
        self.configs: AmortizedInferenceConfigurations
        self.model = self.volume_model

        self.batch_size_known_poses = self.configs.batch_size_known_poses * self.n_prcs
        self.batch_size_hps = self.configs.batch_size_hps * self.n_prcs
        self.batch_size_sgd = self.configs.batch_size_sgd * self.n_prcs

        # tensorboard writer
        self.summaries_dir = os.path.join(self.configs.outdir, "summaries")
        self.writer = None

        # TODO: Replace with DistributedDataParallel
        if self.n_prcs > 1:
            self.model = MyDataParallel(self.volume_model)

        self.model.output_mask.binary_mask = self.model.output_mask.binary_mask.cpu()
        self.optimizers = {"hypervolume": self.volume_optimizer}
        self.optimizer_types = {"hypervolume": self.configs.volume_optim_type}

        # pose table
        if not self.configs.use_gt_poses:
            if self.configs.epochs_sgd > 0:
                pose_table_params = [
                    {"params": list(self.model.pose_table.parameters())}
                ]
                self.optimizers["pose_table"] = self.optim_types[
                    self.configs.pose_table_optim_type
                ](pose_table_params, lr=self.configs.lr_pose_table)
                self.optimizer_types["pose_table"] = self.configs.pose_table_optim_type

        # conformations
        if self.configs.z_dim > 0:
            if self.configs.use_conf_encoder:
                conf_encoder_params = [
                    {
                        "params": (
                            list(self.model.conf_cnn.parameters())
                            + list(self.model.conf_regressor.parameters())
                        )
                    }
                ]

                self.optimizers["conf_encoder"] = self.optim_types[
                    self.configs.conf_encoder_optim_type
                ](
                    conf_encoder_params,
                    lr=self.configs.lr_conf_encoder,
                    weight_decay=self.configs.weight_decay,
                )
                self.optimizer_types[
                    "conf_encoder"
                ] = self.configs.conf_encoder_optim_type

            else:
                conf_table_params = [
                    {"params": list(self.model.conf_table.parameters())}
                ]

                self.optimizers["conf_table"] = self.optim_types[
                    self.configs.conf_table_optim_type
                ](conf_table_params, lr=self.configs.lr_conf_table)
                self.optimizer_types["conf_table"] = self.configs.conf_table_optim_type

        self.optimized_modules = []
        self.data_generators = {"hps": None, "known": None, "sgd": None}

        # dataloaders
        if self.batch_size_hps != self.configs.batch_size:
            self.data_generators["hps"] = make_dataloader(
                self.data,
                batch_size=self.batch_size_hps,
                num_workers=self.configs.num_workers,
                shuffler_size=self.configs.shuffler_size,
            )

        if self.batch_size_known_poses != self.configs.batch_size:
            self.data_generators["known"] = make_dataloader(
                self.data,
                batch_size=self.batch_size_known_poses,
                num_workers=self.configs.num_workers,
                shuffler_size=self.configs.shuffler_size,
            )
        if self.batch_size_sgd != self.configs.batch_size:
            self.data_generators["sgd"] = make_dataloader(
                self.data,
                batch_size=self.batch_size_sgd,
                num_workers=self.configs.num_workers,
                shuffler_size=self.configs.shuffler_size,
            )

        epsilon = 1e-8
        # booleans
        self.log_latents = False
        self.pose_only = True
        self.use_point_estimates = False
        self.first_switch_to_point_estimates = True
        self.first_switch_to_point_estimates_conf = True

        if self.configs.load is not None:
            if self.start_epoch >= self.epochs_pose_search:
                self.first_switch_to_point_estimates = False
            self.first_switch_to_point_estimates_conf = False

        self.use_kl_divergence = (
            not self.configs.z_dim == 0
            and self.configs.variational_het
            and self.configs.beta_conf >= epsilon
        )
        self.use_trans_l1_regularizer = (
            self.configs.trans_l1_regularizer >= epsilon
            and not self.configs.use_gt_trans
            and not self.configs.no_trans
        )
        self.use_l2_smoothness_regularizer = (
            self.configs.l2_smoothness_regularizer >= epsilon
        )

        self.num_epochs = self.epochs_pose_search + self.configs.epochs_sgd
        if self.configs.load:
            self.num_epochs += self.start_epoch

        self.in_dict_last = None
        self.y_pred_last = None
        self.mask_particles_seen_at_last_epoch = np.zeros(self.particle_count)
        self.mask_tilts_seen_at_last_epoch = np.zeros(self.image_count)

        # counters
        self.run_times = {phase: [] for phase in self.run_phases}
        self.batch_idx = None
        self.cur_loss = None
        self.end_time = None

        self.predicted_logvar = (
            np.empty((self.particle_count, self.configs.z_dim))
            if self.configs.z_dim > 0 and self.configs.variational_het
            else None
        )

    def create_outdir(self) -> None:
        super().create_outdir()
        os.makedirs(self.summaries_dir, exist_ok=True)
        self.writer = SummaryWriter(self.summaries_dir)
        self.logger.info("Will write tensorboard summaries " f"in {self.summaries_dir}")

    def begin_epoch(self):
        self.configs: AmortizedInferenceConfigurations
        self.mask_particles_seen_at_last_epoch = np.zeros(self.particle_count)
        self.mask_tilts_seen_at_last_epoch = np.zeros(self.image_count)
        self.optimized_modules = ["hypervolume"]

        self.pose_only = (
            self.total_images_seen < self.configs.pose_only_phase
            or self.configs.z_dim == 0
        )

        if not self.configs.use_gt_poses:
            self.use_point_estimates = self.current_epoch >= max(
                0, self.epochs_pose_search
            )

        # HPS
        if self.in_pose_search_step:
            n_max_particles = self.particle_count
            self.logger.info(f"Will use pose search on {n_max_particles} particles")
            self.data_iterator = self.data_generators["hps"] or self.data_iterator

        # SGD
        elif self.use_point_estimates:
            if self.first_switch_to_point_estimates:
                self.first_switch_to_point_estimates = False
                self.logger.info("Switched to autodecoding poses")

                if self.configs.refine_gt_poses:
                    self.logger.info("Initializing pose table from ground truth")

                    poses_gt = cryodrgn.utils.load_pkl(self.configs.poses)
                    if poses_gt[0].ndim == 3:
                        # contains translations
                        rotmat_gt = torch.tensor(poses_gt[0]).float()
                        trans_gt = torch.tensor(poses_gt[1]).float()
                        trans_gt *= self.resolution

                        if self.ind is not None:
                            rotmat_gt = rotmat_gt[self.ind]
                            trans_gt = trans_gt[self.ind]

                    else:
                        rotmat_gt = torch.tensor(poses_gt).float()
                        trans_gt = None

                        if self.ind is not None:
                            rotmat_gt = rotmat_gt[self.ind]

                    self.model.pose_table.initialize(rotmat_gt, trans_gt)

                else:
                    self.logger.info(
                        "Initializing pose table from hierarchical pose search"
                    )
                    self.model.pose_table.initialize(
                        self.predicted_rots, self.predicted_trans
                    )

                self.model.to(self.device)

            self.logger.info(
                "Will use latent optimization on " f"{self.particle_count} particles"
            )

            self.data_iterator = self.data_generators["sgd"] or self.data_iterator
            self.optimized_modules.append("pose_table")

        # GT poses
        else:
            assert self.configs.use_gt_poses
            self.data_iterator = self.data_generators["known"] or self.data_iterator

        # conformations
        if not self.pose_only:
            if self.configs.use_conf_encoder:
                self.optimized_modules.append("conf_encoder")

            else:
                if self.first_switch_to_point_estimates_conf:
                    self.first_switch_to_point_estimates_conf = False

                    if self.configs.initial_conf is not None:
                        self.logger.info(
                            "Initializing conformation table " "from given z's"
                        )
                        self.model.conf_table.initialize(
                            cryodrgn.utils.load_pkl(self.configs.initial_conf)
                        )

                    self.model.to(self.device)

                self.optimized_modules.append("conf_table")

        for key in self.run_times.keys():
            self.run_times[key] = []

        self.end_time = time.time()

    def end_epoch(self) -> None:
        # update output mask -- epoch-based scaling
        if hasattr(self.model.output_mask, "update_epoch") and self.use_point_estimates:
            self.model.output_mask.update_epoch(self.configs.n_frequencies_per_epoch)

    def get_ctfs_at(self, index):
        batch_size = len(index)
        ctf_params_local = (
            self.ctf_params[index] if self.ctf_params is not None else None
        )

        if ctf_params_local is not None:
            freqs = self.lattice.freqs2d.unsqueeze(0).expand(
                batch_size, *self.lattice.freqs2d.shape
            ) / ctf_params_local[:, 0].view(batch_size, 1, 1)

            ctf_local = ctf.compute_ctf(
                freqs, *torch.split(ctf_params_local[:, 1:], 1, 1)
            ).view(batch_size, self.resolution, self.resolution)

        else:
            ctf_local = None

        return ctf_local

    def train_batch(self, batch: dict[str, torch.Tensor]) -> None:
        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times["dataloading"].append(time.time() - self.end_time)

        # update output mask -- image-based scaling
        if hasattr(self.model.output_mask, "update") and self.in_pose_search_step:
            self.model.output_mask.update(self.total_images_seen)

        if self.in_pose_search_step:
            self.model.ps_params["l_min"] = self.configs.l_start

            if self.configs.output_mask == "circ":
                self.model.ps_params["l_max"] = self.configs.l_end
            else:
                self.model.ps_params["l_max"] = min(
                    self.model.output_mask.current_radius, self.configs.l_end
                )

        if "tilt_indices" not in batch:
            batch["tilt_indices"] = batch["indices"]
        else:
            batch["tilt_indices"] = batch["tilt_indices"].reshape(-1)

        if self.configs.verbose_time:
            torch.cuda.synchronize()
        start_time_gpu = time.time()
        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times["to_gpu"].append(time.time() - start_time_gpu)

        # zero grad
        for key in self.optimized_modules:
            self.optimizers[key].zero_grad()

        # forward pass
        latent_variables_dict, y_pred, y_gt_processed = self.forward_pass(**batch)

        if self.n_prcs > 1:
            self.model.module.is_in_pose_search_step = False
        else:
            self.model.is_in_pose_search_step = False

        # loss
        if self.configs.verbose_time:
            torch.cuda.synchronize()

        start_time_loss = time.time()
        total_loss, all_losses = self.loss(
            y_pred, y_gt_processed, latent_variables_dict
        )

        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times["loss"].append(time.time() - start_time_loss)

        # backward pass
        if self.configs.verbose_time:
            torch.cuda.synchronize()
        start_time_backward = time.time()
        total_loss.backward()
        # self.cur_loss += total_loss.item() * len(indices)

        for key in self.optimized_modules:
            if self.optimizer_types[key] == "adam":
                self.optimizers[key].step()

            elif self.optimizer_types[key] == "lbfgs":

                def closure():
                    self.optimizers[key].zero_grad()
                    (
                        _latent_variables_dict,
                        _y_pred,
                        _y_gt_processed,
                    ) = self.forward_pass(**batch)
                    _loss, _ = self.loss(
                        _y_pred, _y_gt_processed, _latent_variables_dict
                    )
                    _loss.backward()
                    return _loss.item()

                self.optimizers[key].step(closure)

            else:
                raise NotImplementedError

        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times["backward"].append(time.time() - start_time_backward)

        # detach
        if self.will_make_checkpoint:
            self.in_dict_last = batch
            self.y_pred_last = y_pred

            if self.configs.verbose_time:
                torch.cuda.synchronize()

            start_time_cpu = time.time()
            rot_pred, trans_pred, conf_pred, logvar_pred = self.detach_latent_variables(
                latent_variables_dict
            )

            if self.configs.verbose_time:
                torch.cuda.synchronize()
                self.run_times["to_cpu"].append(time.time() - start_time_cpu)

            if self.use_cuda:
                batch["indices"] = batch["indices"].cpu()
                if batch["tilt_indices"] is not None:
                    batch["tilt_indices"] = batch["tilt_indices"].cpu()

            # keep track of predicted variables
            self.mask_particles_seen_at_last_epoch[batch["indices"]] = 1
            self.mask_tilts_seen_at_last_epoch[batch["tilt_indices"]] = 1
            self.predicted_rots[batch["tilt_indices"]] = rot_pred.reshape(-1, 3, 3)
            if not self.configs.no_trans:
                self.predicted_trans[batch["tilt_indices"]] = trans_pred.reshape(-1, 2)
            if self.configs.z_dim > 0:
                self.predicted_conf[batch["indices"]] = conf_pred
                if self.configs.variational_het:
                    self.predicted_logvar[batch["indices"]] = logvar_pred

        else:
            self.run_times["to_cpu"].append(0.0)

        # logging
        self.end_time = time.time()
        all_losses["total"] = total_loss
        for loss_k, loss_val in all_losses.items():
            if loss_k in self.accum_losses:
                self.accum_losses[loss_k] += loss_val * len(batch["indices"])
            else:
                self.accum_losses[loss_k] = loss_val * len(batch["indices"])

    def detach_latent_variables(self, latent_variables_dict):
        rot_pred = latent_variables_dict["R"].detach().cpu().numpy()
        trans_pred = (
            latent_variables_dict["t"].detach().cpu().numpy()
            if not self.configs.no_trans
            else None
        )

        conf_pred = (
            latent_variables_dict["z"].detach().cpu().numpy()
            if self.configs.z_dim > 0 and "z" in latent_variables_dict
            else None
        )

        logvar_pred = (
            latent_variables_dict["z_logvar"].detach().cpu().numpy()
            if self.configs.z_dim > 0 and "z_logvar" in latent_variables_dict
            else None
        )

        return rot_pred, trans_pred, conf_pred, logvar_pred

    def forward_pass(self, y, y_real, tilt_indices, indices):
        if self.configs.verbose_time:
            torch.cuda.synchronize()

        start_time_ctf = time.time()
        if tilt_indices is not None:
            ctf_local = self.get_ctfs_at(tilt_indices)
        else:
            ctf_local = self.get_ctfs_at(indices)

        if self.configs.subtomo_averaging:
            ctf_local = ctf_local.reshape(-1, self.image_count, *ctf_local.shape[1:])

        if self.configs.verbose_time:
            torch.cuda.synchronize()
            self.run_times["ctf"].append(time.time() - start_time_ctf)

        # forward pass
        if "hypervolume" in self.optimized_modules:
            self.model.hypervolume.train()
        else:
            self.model.hypervolume.eval()

        if hasattr(self.model, "conf_cnn"):
            if hasattr(self.model, "conf_regressor"):
                if "conf_encoder" in self.optimized_modules:
                    self.model.conf_cnn.train()
                    self.model.conf_regressor.train()
                else:
                    self.model.conf_cnn.eval()
                    self.model.conf_regressor.eval()

        if hasattr(self.model, "pose_table"):
            if "pose_table" in self.optimized_modules:
                self.model.pose_table.train()
            else:
                self.model.pose_table.eval()

        if hasattr(self.model, "conf_table"):
            if "conf_table" in self.optimized_modules:
                self.model.conf_table.train()
            else:
                self.model.conf_table.eval()

        if self.n_prcs > 1:
            self.model.module.pose_only = self.pose_only
            self.model.module.use_point_estimates = self.use_point_estimates
            self.model.module.pretrain = self.in_pretraining
            self.model.module.is_in_pose_search_step = self.in_pose_search_step
            self.model.module.use_point_estimates_conf = (
                not self.configs.use_conf_encoder
            )

        else:
            self.model.pose_only = self.pose_only
            self.model.use_point_estimates = self.use_point_estimates
            self.model.pretrain = self.in_pretraining
            self.model.is_in_pose_search_step = self.in_pose_search_step
            self.model.use_point_estimates_conf = not self.configs.use_conf_encoder

        if self.configs.subtomo_averaging:
            tilt_indices = tilt_indices.reshape(y.shape[0:2])

        in_dict = {
            "y": y,
            "y_real": y_real,
            "indices": indices,
            "tilt_indices": tilt_indices,
            "ctf": ctf_local,
        }

        if in_dict["tilt_indices"] is None:
            in_dict["tilt_indices"] = in_dict["indices"]
        else:
            in_dict["tilt_indices"] = in_dict["tilt_indices"].reshape(-1)

        out_dict = self.model(in_dict)
        self.run_times["encoder"].append(
            torch.mean(out_dict["time_encoder"].cpu())
            if self.configs.verbose_time
            else 0.0
        )

        self.run_times["decoder"].append(
            torch.mean(out_dict["time_decoder"].cpu())
            if self.configs.verbose_time
            else 0.0
        )

        self.run_times["decoder_coords"].append(
            torch.mean(out_dict["time_decoder_coords"].cpu())
            if self.configs.verbose_time
            else 0.0
        )

        self.run_times["decoder_query"].append(
            torch.mean(out_dict["time_decoder_query"].cpu())
            if self.configs.verbose_time
            else 0.0
        )

        latent_variables_dict = out_dict
        y_pred = out_dict["y_pred"]
        y_gt_processed = out_dict["y_gt_processed"]

        if self.configs.subtomo_averaging and self.configs.dose_exposure_correction:
            mask = self.model.output_mask.binary_mask
            a_pix = self.ctf_params[0, 0]

            dose_filters = self.data.get_dose_filters(
                in_dict["tilt_indices"].reshape(-1), self.lattice, a_pix
            ).reshape(*y_pred.shape[:2], -1)

            y_pred *= dose_filters[..., mask]

        return latent_variables_dict, y_pred, y_gt_processed

    def loss(self, y_pred, y_gt, latent_variables_dict):
        """
        y_pred: [batch_size(, n_tilts), n_pts]
        y: [batch_size(, n_tilts), n_pts]
        """
        all_losses = {}

        # data loss
        data_loss = F.mse_loss(y_pred, y_gt)
        all_losses["Data Loss"] = data_loss.item()
        total_loss = data_loss

        # KL divergence
        if self.use_kl_divergence:
            kld_conf = kl_divergence_conf(latent_variables_dict)
            total_loss += self.configs.beta_conf * kld_conf / self.resolution**2
            all_losses["KL Div. Conf."] = kld_conf.item()

        # L1 regularization for translations
        if self.use_trans_l1_regularizer and self.use_point_estimates:
            trans_l1_loss = l1_regularizer(latent_variables_dict["t"])
            total_loss += self.configs.trans_l1_regularizer * trans_l1_loss
            all_losses["L1 Reg. Trans."] = trans_l1_loss.item()

        # L2 smoothness prior
        if self.use_l2_smoothness_regularizer:
            smoothness_loss = l2_frequency_bias(
                y_pred,
                self.lattice.freqs2d,
                self.model.output_mask.binary_mask,
                self.resolution,
            )
            total_loss += self.configs.l2_smoothness_regularizer * smoothness_loss
            all_losses["L2 Smoothness Loss"] = smoothness_loss.item()

        return total_loss, all_losses

    def save_epoch_data(self):
        summary.make_img_summary(
            self.writer,
            self.in_dict_last,
            self.y_pred_last,
            self.model.output_mask,
            self.current_epoch,
        )

        # conformation
        if self.configs.z_dim > 0:
            labels = None

            if self.configs.labels is not None:
                labels = cryodrgn.utils.load_pkl(self.configs.labels)

                if self.ind is not None:
                    labels = labels[self.ind]

            if self.mask_particles_seen_at_last_epoch is not None:
                mask_idx = self.mask_particles_seen_at_last_epoch > 0.5
            else:
                mask_idx = np.ones((self.particle_count,), dtype=bool)

            predicted_conf = self.predicted_conf[mask_idx]
            labels = labels[mask_idx] if labels is not None else None
            logvar = (
                self.predicted_logvar[mask_idx]
                if self.predicted_logvar is not None
                else None
            )
            summary.make_conf_summary(
                self.writer,
                predicted_conf,
                self.current_epoch,
                labels,
                pca=None,
                logvar=logvar,
                palette_type=self.configs.palette_type,
            )

        # pose
        rotmat_gt = None
        trans_gt = None
        shift = not self.configs.no_trans

        if self.mask_particles_seen_at_last_epoch is not None:
            mask_tilt_idx = self.mask_tilts_seen_at_last_epoch > 0.5
        else:
            mask_tilt_idx = np.ones((self.image_count,), dtype=bool)

        if self.configs.poses is not None:
            poses_gt = cryodrgn.utils.load_pkl(self.configs.poses)

            if poses_gt[0].ndim == 3:
                # contains translations
                rotmat_gt = torch.tensor(poses_gt[0]).float()
                trans_gt = torch.tensor(poses_gt[1]).float() * self.resolution

                if self.ind is not None:
                    rotmat_gt = rotmat_gt[self.ind]
                    trans_gt = trans_gt[self.ind]

            else:
                rotmat_gt = torch.tensor(poses_gt).float()
                trans_gt = None
                assert not shift, "Shift activated but trans not given in gt"

                if self.ind is not None:
                    rotmat_gt = rotmat_gt[self.ind]

            rotmat_gt = rotmat_gt[mask_tilt_idx]
            trans_gt = trans_gt[mask_tilt_idx] if trans_gt is not None else None

        predicted_rots = self.predicted_rots[mask_tilt_idx]
        predicted_trans = (
            self.predicted_trans[mask_tilt_idx]
            if self.predicted_trans is not None
            else None
        )

        summary.make_pose_summary(
            self.writer,
            predicted_rots,
            predicted_trans,
            rotmat_gt,
            trans_gt,
            self.current_epoch,
            shift=shift,
        )

        self.save_latents()
        self.save_volume()
        self.save_model()

    def print_batch_summary(self) -> None:
        self.logger.info(
            f"# [Train Epoch: {self.current_epoch}/{self.num_epochs - 1}] "
            f"[{self.epoch_images_seen}"
            f"/{self.particle_count} particles]"
        )

        if hasattr(self.model.output_mask, "current_radius"):
            self.accum_losses["Mask Radius"] = self.model.output_mask.current_radius
        if self.model.trans_search_factor is not None:
            self.accum_losses["Trans. Search Factor"] = self.model.trans_search_factor

        summary.make_scalar_summary(
            self.writer, self.accum_losses, self.total_images_seen
        )

        if self.configs.verbose_time:
            for key in self.run_times.keys():
                self.logger.info(
                    f"{key} time: {np.mean(np.array(self.run_times[key]))}"
                )

    def save_latents(self):
        """Write model's latent variables to file."""
        out_pose = os.path.join(self.configs.outdir, f"pose.{self.current_epoch}.pkl")

        if self.configs.no_trans:
            with open(out_pose, "wb") as f:
                pickle.dump(self.predicted_rots, f)
        else:
            with open(out_pose, "wb") as f:
                pickle.dump((self.predicted_rots, self.predicted_trans), f)

        if self.configs.z_dim > 0:
            out_conf = os.path.join(
                self.configs.outdir, f"conf.{self.current_epoch}.pkl"
            )
            with open(out_conf, "wb") as f:
                pickle.dump(self.predicted_conf, f)

    def save_volume(self):
        """Write reconstructed volume to file."""
        out_mrc = os.path.join(
            self.configs.outdir, f"reconstruct.{self.current_epoch}.mrc"
        )

        self.model.hypervolume.eval()
        if hasattr(self.model, "conf_cnn"):
            if hasattr(self.model, "conf_regressor"):
                self.model.conf_cnn.eval()
                self.model.conf_regressor.eval()

        if hasattr(self.model, "pose_table"):
            self.model.pose_table.eval()
        if hasattr(self.model, "conf_table"):
            self.model.conf_table.eval()

        if self.configs.z_dim > 0:
            zval = self.predicted_conf[0].reshape(-1)
        else:
            zval = None

        vol = -1.0 * self.model.eval_volume(norm=self.data.norm, zval=zval)
        MRCFile.write(out_mrc, np.array(vol, dtype=np.float32))

    # TODO: weights -> model and reconstruct -> volume for output labels?
    def save_model(self):
        """Write model state to file."""
        out_weights = os.path.join(
            self.configs.outdir, f"weights.{self.current_epoch}.pkl"
        )

        optimizers_state_dict = {}
        for key in self.optimizers.keys():
            optimizers_state_dict[key] = self.optimizers[key].state_dict()

        saved_objects = {
            "epoch": self.current_epoch,
            "model_state_dict": (
                self.model.module.state_dict()
                if self.n_prcs > 1
                else self.model.state_dict()
            ),
            "hypervolume_state_dict": (
                self.model.module.hypervolume.state_dict()
                if self.n_prcs > 1
                else self.model.hypervolume.state_dict()
            ),
            "hypervolume_params": self.model.hypervolume.get_building_params(),
            "optimizers_state_dict": optimizers_state_dict,
        }

        if hasattr(self.model.output_mask, "current_radius"):
            saved_objects["output_mask_radius"] = self.model.output_mask.current_radius

        torch.save(saved_objects, out_weights)

    @property
    def in_pose_search_step(self) -> bool:
        in_pose_search = False

        if not self.configs.use_gt_poses:
            in_pose_search = 1 <= self.current_epoch <= self.epochs_pose_search

        return in_pose_search
