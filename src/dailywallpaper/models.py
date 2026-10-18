# src/dailywallpaper/models.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dailywallpaper.analysis.vision import VisionScores


@dataclass(frozen=True)
class Decision:
    wallpaper_id: int
    image_path: Path
    scores: "VisionScores"
    explore: float
    personalize: float
    final: float
    reason: str
    source: str = ""
    reviewed: bool = False
    apply_path: Path | None = None
    # A remote Steam item is only ready once its project.json exists locally.
    ready: bool = True
