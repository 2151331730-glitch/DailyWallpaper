# src/dailywallpaper/analysis/desktop_fit.py
"""How well a picture works as a desktop background -- measured locally.

The vision model judges the picture; this judges the desk it lands on. A
bright or busy image hides white desktop icons and makes the taskbar look
wrong, however good it is in isolation. Everything here is plain PIL/numpy,
so it costs nothing and can run on every candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

SAMPLE_SIZE = (320, 180)
LUMA_WEIGHTS = (0.299, 0.587, 0.114)
# Above this mean brightness, white icon labels start to disappear.
TOO_BRIGHT = 170.0
# Below this the desktop reads as a black hole; a little lift is welcome.
TOO_DARK = 35.0
# Share of pixels on a strong edge; above it the icons get lost in detail.
BUSY_EDGE = 0.30
# The taskbar strip at the bottom of the screen.
BOTTOM_BRIGHT = 190.0
BOTTOM_SHARE = 0.12


@dataclass(frozen=True)
class DesktopFit:
    mean_luma: float
    luma_std: float
    edge_density: float
    bottom_luma: float
    score: float


def measure(path: Path) -> DesktopFit:
    """Measure one image (a 320x180 downsample is plenty)."""
    with Image.open(path) as image:
        sample = image.convert("RGB").resize(SAMPLE_SIZE)
    pixels = np.asarray(sample, dtype=np.float32)
    luma = (
        pixels[..., 0] * LUMA_WEIGHTS[0]
        + pixels[..., 1] * LUMA_WEIGHTS[1]
        + pixels[..., 2] * LUMA_WEIGHTS[2]
    )
    mean = float(luma.mean())
    std = float(luma.std())
    gy, gx = np.gradient(luma)
    edge_density = float((np.hypot(gx, gy) > 18).mean())
    bottom_start = int(luma.shape[0] * (1 - BOTTOM_SHARE))
    bottom = float(luma[bottom_start:].mean())
    return DesktopFit(
        mean_luma=mean,
        luma_std=std,
        edge_density=edge_density,
        bottom_luma=bottom,
        score=score(mean, bottom, edge_density),
    )


def score(mean: float, bottom: float, edge_density: float) -> float:
    """0-100; 100 is a quiet, mid-tone picture that leaves the icons readable."""
    result = 100.0
    if mean > TOO_BRIGHT:
        result -= (mean - TOO_BRIGHT) * 0.7
    elif mean < TOO_DARK:
        result -= (TOO_DARK - mean) * 0.4
    if bottom > BOTTOM_BRIGHT:
        result -= (bottom - BOTTOM_BRIGHT) * 0.5
    if edge_density > BUSY_EDGE:
        result -= (edge_density - BUSY_EDGE) * 150
    return max(0.0, min(100.0, result))
