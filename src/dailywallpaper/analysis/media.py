# src/dailywallpaper/analysis/media.py
"""Turn a video/GIF into a single still the vision model can score."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image

THUMB_HEIGHT = 360
FRAME_INTERVAL = 2          # seconds between sampled frames
FRAMES_PER_SHEET = 3
FFMPEG_TIMEOUT = 120


class MediaError(RuntimeError):
    pass


def gif_first_frame(src: Path, dest: Path) -> Path:
    """Use the first frame: a looping GIF has no meaningful "10%" frame."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im.seek(0)
        im.convert("RGB").save(dest, "JPEG", quality=90)
    return dest


def video_contact_sheet(video: Path, dest: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Sample the clip every two seconds and tile three frames side by side.

    Sampling by rate rather than by timestamp keeps the command deterministic
    without first probing the duration; a very short clip simply yields fewer
    frames in the sheet.

    A sheet that is at least as new as the clip is reused: decoding a 4K video
    takes seconds, and every prepare pass would otherwise pay it again.
    """
    try:
        if dest.is_file() and dest.stat().st_mtime >= video.stat().st_mtime:
            return dest
    except OSError:
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    filters = (
        f"fps=1/{FRAME_INTERVAL},scale=-1:{THUMB_HEIGHT},"
        f"tile={FRAMES_PER_SHEET}x1"
    )
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-vf",
            filters,
            "-frames:v",
            "1",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT,
        # The app runs from ``pythonw`` (no console of its own), so a child
        # without this flag gets a brand-new black window. Every other spawn
        # in the tree already passes it; this one was the last leak.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not dest.is_file():
        raise MediaError(f"ffmpeg failed: {result.stderr[-300:]}")
    return dest
