# src/dailywallpaper/sources/bing.py
"""Bing daily wallpaper adapter (one UHD image per day)."""
from __future__ import annotations

import requests

from .base import Candidate, SourceError

BASE_URL = "https://www.bing.com"
ARCHIVE_URL = f"{BASE_URL}/HPImageArchive.aspx"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
UHD_WIDTH = 3840
UHD_HEIGHT = 2160


class BingSource:
    """Fetch the current Bing daily image as a single candidate.

    The UHD asset is derived as ``urlbase + "_UHD.jpg"``. If ``urlbase`` is
    missing the UHD image is treated as unavailable and the day is skipped
    (no 1080p fallback), per the design spec.
    """

    def __init__(self, session: requests.Session):
        self.session = session

    def fetch(self) -> list[Candidate]:
        try:
            response = self.session.get(
                ARCHIVE_URL,
                params={"format": "js", "idx": 0, "n": 1, "mkt": "zh-CN"},
                headers={"User-Agent": BROWSER_USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SourceError(f"bing request failed: {exc}") from exc
        if response.status_code == 429:
            raise SourceError("bing rate limit hit (HTTP 429)")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise SourceError(f"bing HTTP {response.status_code}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("bing returned malformed JSON") from exc
        images = payload.get("images") if isinstance(payload, dict) else None
        if not isinstance(images, list) or not images:
            return []
        image = images[0]
        if not isinstance(image, dict):
            raise SourceError("bing image entry is not an object")
        urlbase = image.get("urlbase")
        if not isinstance(urlbase, str) or not urlbase.strip():
            # UHD is unavailable; skip the day rather than downgrade.
            return []
        file_url = f"{BASE_URL}{urlbase}_UHD.jpg"
        preview = image.get("url")
        preview_url = (
            f"{BASE_URL}{preview}" if isinstance(preview, str) and preview.startswith("/")
            else (preview if isinstance(preview, str) and preview else file_url)
        )
        return [
            Candidate(
                source="bing",
                source_id=str(
                    image.get("hsh")
                    or image.get("fullstartdate")
                    or image.get("startdate")
                    or ""
                ),
                title=str(image.get("title") or image.get("copyright") or ""),
                page_url=str(image.get("copyrightlink") or BASE_URL),
                file_url=file_url,
                preview_url=preview_url,
                media_type="image",
                width=UHD_WIDTH,
                height=UHD_HEIGHT,
                file_size=0,
                tags=[],
                raw=image,
            )
        ]
