import json
import os
import typing
from abc import ABC, ABCMeta, abstractmethod
from pathlib import Path

import numpy as np
import torch


class FeatureDataloader(ABC):
    def __init__(
            self,
            cfg: dict,
            device: torch.device,
            image_list: torch.Tensor, # (N, 3, H, W)
            cache_path: Path,
    ):
        self.cfg = cfg
        self.device = device
        self.cache_path = cache_path
        self.data = None # only expect data to be cached, nothing else
        self.try_load(image_list) # don't save image_list, avoid duplicates

    @abstractmethod
    def __call__(self, img_points):
        # img_points: (B, 3) # (img_ind, x, y)
        pass

    @abstractmethod
    def create(self, image_list: torch.Tensor):
        pass

    def load(self):
        cache_info_path = self.cache_path.with_suffix(".info")
        if not cache_info_path.exists():
            raise FileNotFoundError(f"Cache info not found: {cache_info_path}")
        else:
            print(f"Loading cache from {self.cache_path} with config {self.cfg}")
        with open(cache_info_path, "r") as f:
            cfg = json.loads(f.read())
        if cfg != self.cfg:
            raise ValueError(f"Config mismatch: {cfg=} != {self.cfg=}")
        self.data = torch.from_numpy(np.load(self.cache_path)).to(self.device)

    def save(self):
        os.makedirs(self.cache_path.parent, exist_ok=True)
        cache_info_path = self.cache_path.with_suffix(".info")
        print(f"Saving cache to {self.cache_path} with config {self.cfg}")
        with open(cache_info_path, "w") as f:
            f.write(json.dumps(self.cfg))
        np.save(self.cache_path, self.data)

    def try_load(self, img_list: torch.Tensor):
        try:
            self.load()
        except (FileNotFoundError, ValueError) as e:
            s_str = str(e)
            print(f"Failed to load cache: {s_str}")
            self.create(img_list)
            self.save()