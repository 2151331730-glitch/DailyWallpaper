# src/dailywallpaper/sources/steam_workshop.py
"""Wallpaper Engine items from the online Steam workshop.

This source only discovers metadata and a preview: the workshop file itself is
downloaded later through SteamCMD (see ``dailywallpaper.steamcmd``). A
candidate is *ready* once its ``project.json`` exists under the download root.
"""
from __future__ import annotations

import html as html_module
import logging
import re
from pathlib import Path

import requests

from .base import Candidate, SourceError

logger = logging.getLogger(__name__)

APP_ID = 431960
QUERY_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
# api.steampowered.com drops its TLS handshake from mainland China even with a
# proxy; the community workshop page loads fine, so it is the fallback.
BROWSE_URL = "https://steamcommunity.com/workshop/browse/"
PAGE_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
MATURE = {"mature", "questionable"}
_BROWSE_ID = re.compile(r"filedetails/\?id=(\d+)")
_BROWSE_IMG = re.compile(r'<img[^>]+src="([^"]+)"[^>]*alt="([^"]*)"')
BROWSE_WINDOW = 2500


def parse_browse(html: str, limit: int = 50) -> list[dict]:
    """Pull item ids, titles and previews out of the workshop listing page.

    The page renders each card as a link followed by its preview image and
    title, so one anchor plus the next image is enough; nothing is executed.
    """
    found: list[dict] = []
    seen: set[str] = set()
    for match in _BROWSE_ID.finditer(html):
        file_id = match.group(1)
        if file_id in seen:
            continue
        image = _BROWSE_IMG.search(html, match.end(), match.end() + BROWSE_WINDOW)
        if image is None:
            continue
        preview = html_module.unescape(image.group(1)).split("?")[0]
        title = html_module.unescape(image.group(2))
        seen.add(file_id)
        found.append(
            {
                "publishedfileid": file_id,
                "title": title,
                "preview_url": preview,
                "tags": [],
                "file_size": 0,
            }
        )
        if len(found) >= limit:
            break
    return found


class SteamWorkshopClient:
    """One QueryFiles call: the workshop listing for Wallpaper Engine."""

    def __init__(self, api_key: str, session=None, timeout: int = 10, url: str = QUERY_URL):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.url = url

    def browse(self, days: int = 30, per_page: int = 50,
               search_text: str | None = None) -> list[dict]:
        """The public workshop listing, without the Web API."""
        params = {
            "appid": APP_ID,
            "browsesort": "trend",
            "days": days,
            "p": 1,
        }
        if search_text:
            params["searchtext"] = search_text
        response = self.session.get(
            BROWSE_URL,
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        items = parse_browse(response.text, limit=per_page)
        if not items:
            raise SourceError("workshop browse page returned no items")
        return items

    def query(self, query_type: int = 3, days: int = 30, per_page: int = 20,
              page: int = 1, search_text: str | None = None) -> list[dict]:
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
        if search_text:
            # Steam's search_text accepts Chinese titles as well as English.
            params["search_text"] = search_text
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

    def fetch(self, limit: int | None = None,
              query: str | None = None) -> list[Candidate]:
        try:
            details = self.client.query(
                query_type=self.query_type, days=self.days,
                per_page=self.per_page, search_text=query,
            )
        except Exception as exc:
            # The API host is unreachable from here even through a proxy; the
            # public listing is the same workshop and needs no API key.
            logger.warning(
                f"steam api unavailable ({exc}); reading the workshop page"
            )
            details = self.client.browse(
                days=self.days, per_page=self.per_page, search_text=query
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
