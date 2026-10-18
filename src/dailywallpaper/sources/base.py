# src/dailywallpaper/sources/base.py
"""Common source-adapter types."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """Metadata for one wallpaper candidate.

    Images are not downloaded while building a candidate; ``file_url`` is the
    original-resolution asset and ``preview_url`` is the (usually thumbnail)
    asset used for initial screening.
    """

    source: str
    source_id: str
    title: str
    page_url: str
    file_url: str
    preview_url: str
    media_type: str
    width: int
    height: int
    file_size: int
    tags: list[str]
    raw: dict
    content_rating: str = ""


class SourceError(RuntimeError):
    """Raised when a source is unavailable or returns unusable data."""
