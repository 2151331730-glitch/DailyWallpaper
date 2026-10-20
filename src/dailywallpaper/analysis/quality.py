# src/dailywallpaper/analysis/quality.py
"""Last-line inspection of the image that is about to hit the desktop.

The vision model judges aesthetics from a 1024px screening copy, and the
screening copy must never be applied: doing so on a 2560x1440 desktop looked
like a blurry mess. This module answers one question with pixels: is this file
sharp and big enough to be a wallpaper?
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

# A wallpaper may be smaller than the screen (Windows scales it), but not by
# much; below this share of the screen it visibly softens at 1:1 viewing.
MIN_SCREEN_SCALE = 0.75
# Laplacian variance of a 512px native crop, reported for diagnostics. The
# floor only catches images with no detail at all: minimalist gradients,
# vector art and soft fogginess are legitimate wallpapers, and the vision
# quality score already judges them. Resolution is the hard gate.
SHARPNESS_FLOOR = 1.0
SHARPNESS_CROP = 512


@dataclass(frozen=True)
class Inspection:
    width: int
    height: int
    sharpness: float
    reasons: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.reasons


def screen_size() -> tuple[int, int]:
    """The primary monitor's pixels, with a sane fallback off Windows."""
    try:
        user32 = ctypes.windll.user32
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        if width > 0 and height > 0:
            return int(width), int(height)
    except (AttributeError, OSError):
        pass
    return 1920, 1080


def required_resolution(
    screen: tuple[int, int] | None = None, scale: float = MIN_SCREEN_SCALE
) -> tuple[int, int]:
    width, height = screen or screen_size()
    return int(width * scale), int(height * scale)


def sharpness(path: Path, size: int = SHARPNESS_CROP) -> float:
    """Variance of the Laplacian on a native-resolution centre crop.

    Everything is measured at the same 512px scale; shrinking a 4K file first
    would smooth it and make a sharp photo look soft.
    """
    with Image.open(path) as image:
        grey = image.convert("L")
        width, height = grey.size
        if width >= size and height >= size:
            left, top = (width - size) // 2, (height - size) // 2
            grey = grey.crop((left, top, left + size, top + size))
        else:
            grey = grey.resize((size, size), Image.Resampling.LANCZOS)
        pixels = np.asarray(grey, dtype=np.float32)
    if pixels.shape[0] < 3 or pixels.shape[1] < 3:
        return 0.0
    laplacian = (
        -4 * pixels[1:-1, 1:-1]
        + pixels[:-2, 1:-1]
        + pixels[2:, 1:-1]
        + pixels[1:-1, :-2]
        + pixels[1:-1, 2:]
    )
    return float(laplacian.var())


def inspect_image(
    path: Path,
    min_size: tuple[int, int] | None = None,
    sharpness_floor: float = SHARPNESS_FLOOR,
) -> Inspection:
    """Measure a file; unreadable files are reported, never raised."""
    path = Path(path)
    try:
        with Image.open(path) as image:
            width, height = image.size
    except OSError as exc:
        return Inspection(0, 0, 0.0, (f"unreadable: {exc}",))
    reasons: list[str] = []
    if min_size is not None:
        min_width, min_height = min_size
        if width < min_width or height < min_height:
            reasons.append("resolution")
    try:
        score = sharpness(path)
    except OSError:
        score = 0.0
    if score < sharpness_floor:
        reasons.append("blurry")
    return Inspection(width, height, score, tuple(reasons))
