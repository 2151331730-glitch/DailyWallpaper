# src/dailywallpaper/analysis/calibration.py
"""Put contact-sheet scores back on the single-picture scale.

A video or GIF candidate is scored from a contact sheet (three frames tiled /
the first frame), and the vision model reads that shape systematically lower
than it reads a plain wallpaper. docs/DYNAMIC-COMPARABILITY.md measured the
bias on 24 live wallpapers against a re-encoded noise floor:

    creativity −7.5, novelty −2.5, formulaic +1.2 (video)

The offsets below add back the direction and rough size of that gap. They are
applied only to previews that are contact sheets / extracted frames -- the
author's own cover image was measured identical (a cache hit, but it is also
the same kind of input a static wallpaper gives the model), so calibrating it
would bias the other way.

The numbers are a direction, not a precise correction: 12 videos and 12 GIFs
carry about ±3 points of standard error. Re-measure before changing them.
"""
from __future__ import annotations

from dataclasses import replace

from .vision import VisionScores

# Added to the contact-sheet scores before ranking. formulaic is a penalty
# input, so its sign is inverted relative to the other two.
DIMENSION_OFFSETS = {"creativity": 5.8, "novelty": 3.1, "formulaic": -2.1}


def _move(value: float, offset: float) -> int:
    return max(0, min(100, round(value + offset)))


def calibrate_scores(scores: VisionScores) -> VisionScores:
    """Return the same scores with the contact-sheet bias compensated.

    Only the three measured dimensions move; quality is a hard gate measured
    the same way in both shapes, and the tags feed the sugar model, which
    was trained on tags -- neither should be rewritten by a measurement of
    the image shape.
    """
    return replace(
        scores,
        **{
            name: _move(getattr(scores, name), offset)
            for name, offset in DIMENSION_OFFSETS.items()
        },
    )
