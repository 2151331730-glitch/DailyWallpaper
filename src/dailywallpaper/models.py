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
    # The decisions row, so a successful apply can promote it to 'applied'.
    decision_id: int | None = None
    # "image" | "scene" | "video": the media plan and the applied-type history
    # need to know which pool a decision came from.
    media_type: str = "image"
    # True when the vision scores came from a contact sheet / extracted GIF
    # frame rather than the author's cover. Only those carry the measured
    # dynamic bias (analysis/calibration.py), so only those are calibrated.
    calibrated: bool = False
    # The source's own title. The sugar-word check looks at it, and the review
    # pass re-scores the same decision -- without this the named words found in
    # the title were dropped on the second pass and the penalty vanished.
    title: str = ""
