# src/dailywallpaper/sources/steam_workshop.py
"""Wallpaper Engine items from the online Steam workshop.

This source only discovers metadata and a preview: the workshop file itself is
downloaded later through SteamCMD (see ``dailywallpaper.steamcmd``). A
candidate is *ready* once its ``project.json`` exists under the download root.
"""
from __future__ import annotations

from pathlib import Path

import requests

from .base import Candidate, SourceError

APP_ID = 431960
QUERY_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
PAGE_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
MATURE = {"mature", "questionable"}


class SteamWorkshopClient:
    """One QueryFiles call: the workshop listing for Wallpaper Engine."""

    def __init__(self, api_key: str, session=None, timeout: int = 10, url: str = QUERY_URL):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.url = url

    def query(self, query_type: int = 3, days: int = 30, per_page: int = 20,
              page: int = 1) -> list[dict]:
        params = {
            "key": self.api_key,
            "appid": APP_ID,
            "query_type": query_type,
            "page": page,
            "numperpage": per_page,
            "days": days,
            "return_short_description": "true",
            "return_metadata": "true",
            # Tags are omitted unless asked for; without them the mature
            # filter and the scene/video split silently do nothing.
            "return_tags": "true",
        }
        response = self.session.get(self.url, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        details = (payload.get("response") or {}).get("publishedfiledetails")
        if not isinstance(details, list):
            raise SourceError("steam returned no publishedfiledetails")
        return details


def _tag_names(item: dict) -> list[str]:
    names = []
    for tag in item.get("tags") or []:
        if isinstance(tag, dict):
            name = tag.get("tag") or tag.get("display_name") or ""
        else:
            name = str(tag)
        if name:
            names.append(str(name))
    return names


def _media_type(tags: list[str]) -> str:
    lowered = {tag.lower() for tag in tags}
    return "video" if "video" in lowered else "scene"


class SteamWorkshopSource:
    """Turn QueryFiles entries into candidates (preview only, no download)."""

    def __init__(self, client, download_root: Path, allow_mature: bool = False,
                 query_type: int = 3, days: int = 30, per_page: int = 20):
        self.client = client
        self.download_root = Path(download_root)
        self.allow_mature = allow_mature
        self.query_type = query_type
        self.days = days
        self.per_page = per_page

    def fetch(self, limit: int | None = None) -> list[Candidate]:
        details = self.client.query(
            query_type=self.query_type, days=self.days, per_page=self.per_page
        )
        found = [c for c in (self._candidate(item) for item in details) if c]
        return found if limit is None else found[:limit]

    def _candidate(self, item: dict) -> Candidate | None:
        file_id = str(item.get("publishedfileid") or "")
        if not file_id:
            return None
        # visibility 0 is public; anything else needs the owner's session.
        if item.get("visibility") not in (None, 0):
            return None
        if item.get("banned"):
            return None
        tags = _tag_names(item)
        mature = any(tag.lower() in MATURE for tag in tags)
        if mature and not self.allow_mature:
            return None
        preview = str(item.get("preview_url") or "")
        if not preview:
            return None
        return Candidate(
            source="steam_workshop",
            source_id=file_id,
            title=str(item.get("title") or ""),
            page_url=PAGE_URL.format(file_id),
            file_url=str(self.download_root / file_id / "project.json"),
            preview_url=preview,
            media_type=_media_type(tags),
            width=0,
            height=0,
            file_size=int(item.get("file_size") or 0),
            tags=tags,
            raw=item,
            content_rating="mature" if mature else "",
        )
