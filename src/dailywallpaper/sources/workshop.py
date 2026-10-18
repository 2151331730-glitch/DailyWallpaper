# src/dailywallpaper/sources/workshop.py
"""Scan the local Wallpaper Engine workshop.

This source touches no network at all: previews, projects and assets are
already on disk, which keeps the daily run working without a proxy.
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import Candidate

MEDIA_TYPES = {"scene": "scene", "video": "video", "web": "scene"}
MATURE = {"mature", "questionable"}
DEFAULT_PREVIEW = "preview.jpg"


class WorkshopSource:
    def __init__(self, workshop_root: Path, allow_mature: bool = False):
        self.root = Path(workshop_root)
        self.allow_mature = allow_mature

    def fetch(self, limit: int | None = None) -> list[Candidate]:
        """Adapter interface: the pipeline calls fetch(), not scan()."""
        items = self.scan()
        return items if limit is None else items[:limit]

    def scan(self) -> list[Candidate]:
        if not self.root.exists():
            return []
        found = []
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir():
                continue
            candidate = self._candidate(folder)
            if candidate is not None:
                found.append(candidate)
        return found

    def _candidate(self, folder: Path) -> Candidate | None:
        manifest = folder / "project.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        rating = str(data.get("contentrating") or "").lower()
        if rating in MATURE and not self.allow_mature:
            return None

        preview = Path(str(data.get("preview") or DEFAULT_PREVIEW))
        preview_path = folder / preview
        if not preview_path.is_file():
            return None

        asset = str(data.get("file") or "")
        if not asset:
            return None
        asset_path = folder / asset

        return Candidate(
            source="we_workshop",
            source_id=folder.name,
            title=str(data.get("title") or ""),
            page_url="",
            file_url=str(asset_path),
            preview_url=str(preview_path),
            media_type=MEDIA_TYPES.get(str(data.get("type") or ""), "scene"),
            width=0,
            height=0,
            file_size=asset_path.stat().st_size if asset_path.is_file() else 0,
            tags=[str(tag) for tag in (data.get("tags") or [])],
            raw=data,
            content_rating=rating,
        )
