# src/dailywallpaper/analysis/prefilter.py
"""Cheap, non-model checks that reject hopeless candidates before analysis."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .images import dhash, hamming

BLUR_VARIANCE = 100.0
EDGE_FRACTION = 0.05
BLACK_LEVEL = 10
BLACK_STRIP_RATIO = 0.15
DUPLICATE_DISTANCE = 8
MIN_DECODED_SIDE = 32


@dataclass(frozen=True)
class PrefilterResult:
    ok: bool
    reasons: list[str]
    dhash: str


def prefilter(path: Path, candidate, cfg, recent_hashes) -> PrefilterResult:
    """Return every reason a candidate fails; ``ok`` is true when there are none."""
    reasons: list[str] = []

    if candidate.media_type == "image":
        if candidate.width < cfg.min_width or candidate.height < cfg.min_height:
            reasons.append("resolution")
        ratio = candidate.width / candidate.height if candidate.height else 0
        if not (cfg.aspect_min <= ratio <= cfg.aspect_max):
            reasons.append("aspect")
    # scene/video carry no preview dimensions; spec 8.2 says not to hard-filter
    # them by aspect (a scene is rendered live and a video is measured later).

    with Image.open(path) as im:
        if im.width < MIN_DECODED_SIDE or im.height < MIN_DECODED_SIDE:
            reasons.append("decode")
        gray = np.asarray(im.convert("L"), dtype=np.float32)
        gy, gx = np.gradient(gray)
        if float((gx * gx + gy * gy).var()) < BLUR_VARIANCE:
            reasons.append("blur")
        height, width = gray.shape
        strips = [
            gray[: max(1, int(height * EDGE_FRACTION)), :],
            gray[-max(1, int(height * EDGE_FRACTION)) :, :],
            gray[:, : max(1, int(width * EDGE_FRACTION))],
            gray[:, -max(1, int(width * EDGE_FRACTION)) :],
        ]
        if any(float((strip < BLACK_LEVEL).mean()) > BLACK_STRIP_RATIO for strip in strips):
            reasons.append("black_bars")

    digest = dhash(path)
    if any(hamming(digest, other) < DUPLICATE_DISTANCE for other in recent_hashes):
        reasons.append("duplicate")

    return PrefilterResult(not reasons, reasons, digest)
