# Copyright 2022 The Nerfstudio Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Datamanager.
"""

from __future__ import annotations

import os.path as osp
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Type, Union

import torch
import yaml
from nerfstudio.cameras.rays import RayBundle
from nerfstudio.data.utils.nerfstudio_collate import nerfstudio_collate
from nerfstudio.engine.callbacks import TrainingCallback, TrainingCallbackAttributes
from nerfstudio.model_components.ray_generators import RayGenerator
from nerfstudio.utils.misc import IterableWrapper
from rich.progress import Console
    
CONSOLE = Console(width=120)

from lerf.data.utils.dino_dataloader import DinoDataloader,DinoV2DataLoader
from lerf.data.utils.pyramid_embedding_dataloader import PyramidEmbeddingDataloader
from lerf.encoders.image_encoder import BaseImageEncoder
from nerfstudio.data.datamanagers.base_datamanager import VanillaDataManager, VanillaDataManagerConfig
from nerfstudio.data.datamanagers.full_images_datamanager import FullImageDatamanager, FullImageDatamanagerConfig
from nerfstudio.cameras.cameras import Cameras
@dataclass
class LERFDataManagerConfig(VanillaDataManagerConfig):
    _target: Type = field(default_factory=lambda: LERFDataManager)
    patch_tile_size_range: Tuple[int, int] = (0.05, 0.5)
    patch_tile_size_res: int = 7
    patch_stride_scaler: float = 0.5

@dataclass
class DiGDataManagerConfig(FullImageDatamanagerConfig):
    _target: Type = field(default_factory=lambda: DiGDataManager)
    use_denoiser:bool = True
    
class DiGDataManager(FullImageDatamanager):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        cache_dir = f"outputs/{self.config.dataparser.data.name}"
        
        if self.config.use_denoiser:
            dino_cache_path = Path(osp.join(cache_dir, "denoised_dino.npy"))
        else:
            dino_cache_path = Path(osp.join(cache_dir, "dino.npy"))
        images = [self.cached_train[i]["image"].permute(2, 0, 1)[None, ...] for i in range(len(self.train_dataset))]
        images = torch.cat(images)
        self.dino_dataloader = DinoDataloader(
            image_list = images,
            device = self.device,
            cfg={"image_shape": list(images.shape[2:4])},
            cache_path=dino_cache_path,
            use_denoiser=self.config.use_denoiser,
        )

    def next_train(self, step: int) -> Tuple[Cameras, Dict]:
        camera, data = super().next_train(step)
        data["dino"] = self.dino_dataloader.get_full_img_feats(camera.metadata["cam_idx"])
        return camera, data
    
class LERFDataManager(VanillaDataManager):  # pylint: disable=abstract-method
    """Basic stored data manager implementation.

    This is pretty much a port over from our old dataloading utilities, and is a little jank
    under the hood. We may clean this up a little bit under the hood with more standard dataloading
    components that can be strung together, but it can be just used as a black box for now since
    only the constructor is likely to change in the future, or maybe passing in step number to the
    next_train and next_eval functions.

    Args:
        config: the DataManagerConfig used to instantiate class
    """

    config: LERFDataManagerConfig

    def __init__(
        self,
        config: LERFDataManagerConfig,
        device: Union[torch.device, str] = "cpu",
        test_mode: Literal["test", "val", "inference"] = "val",
        world_size: int = 1,
        local_rank: int = 0,
        **kwargs,  # pylint: disable=unused-argument
    ):
        super().__init__(
            config=config, device=device, test_mode=test_mode, world_size=world_size, local_rank=local_rank, **kwargs
        )
        self.image_encoder: BaseImageEncoder = kwargs["image_encoder"]
        images = [self.train_dataset[i]["image"].permute(2, 0, 1)[None, ...] for i in range(len(self.train_dataset))]
        images = torch.cat(images)

        cache_dir = f"outputs/{self.config.dataparser.data.name}"
        clip_cache_path = Path(osp.join(cache_dir, f"clip_{self.image_encoder.name}"))
        dino_cache_path = Path(osp.join(cache_dir, "dino.npy"))
        # NOTE: cache config is sensitive to list vs. tuple, because it checks for dict equality
        self.dino_dataloader = DinoDataloader(
            image_list=images,
            device=self.device,
            cfg={"image_shape": list(images.shape[2:4])},
            cache_path=dino_cache_path,
        )
        torch.cuda.empty_cache()
        self.clip_interpolator = PyramidEmbeddingDataloader(
            image_list=images,
            device=self.device,
            cfg={
                "tile_size_range": [0.05, 0.5],
                "tile_size_res": 7,
                "stride_scaler": 0.5,
                "image_shape": list(images.shape[2:4]),
                "model_name": self.image_encoder.name,
            },
            cache_path=clip_cache_path,
            model=self.image_encoder,
        )

    def next_train(self, step: int) -> Tuple[RayBundle, Dict]:
        """Returns the next batch of data from the train dataloader."""
        self.train_count += 1
        image_batch = next(self.iter_train_image_dataloader)
        assert self.train_pixel_sampler is not None
        batch = self.train_pixel_sampler.sample(image_batch)
        ray_indices = batch["indices"]
        ray_bundle = self.train_ray_generator(ray_indices)
        batch["clip"], clip_scale = self.clip_interpolator(ray_indices)
        batch["dino"] = self.dino_dataloader(ray_indices)
        ray_bundle.metadata["clip_scales"] = clip_scale
        # assume all cameras have the same focal length and image width
        ray_bundle.metadata["fx"] = self.train_dataset.cameras[0].fx.item()
        ray_bundle.metadata["width"] = self.train_dataset.cameras[0].width.item()
        ray_bundle.metadata["fy"] = self.train_dataset.cameras[0].fy.item()
        ray_bundle.metadata["height"] = self.train_dataset.cameras[0].height.item()
        return ray_bundle, batch
