# src/dailywallpaper/sources/bing.py
"""Bing daily wallpaper adapter (one UHD image per day)."""
from __future__ import annotations

import logging
import time

import requests

from .base import Candidate, SourceError

logger = logging.getLogger(__name__)

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
# One more attempt after a short pause. The archive endpoint occasionally
# answers 200 with an HTML error page while the same request works a moment
# later, and Bing is the only source that needs no proxy here -- losing it for
# half an hour (SOURCE_RETRY_SECONDS) over one bad response costs the whole day's
# domestic candidate. 429 and other 4xx are not retried: they mean "stop asking".
RETRY_DELAY_SECONDS = 1.5


class _TransientError(Exception):
    """A failure worth one more attempt: dropped connection, 5xx, HTML body."""


class BingSource:
    """Fetch the current Bing daily image as a single candidate.

    The UHD asset is derived as ``urlbase + "_UHD.jpg"``. If ``urlbase`` is
    missing the UHD image is treated as unavailable and the day is skipped
    (no 1080p fallback), per the design spec.
    """

    def __init__(self, session: requests.Session, config=None):
        self.session = session
        self.config = config

    def _archive_payload(self):
        """The decoded archive response, with one retry on a transient failure."""
        try:
            return self._request_once()
        except _TransientError as exc:
            logger.warning(f"bing request failed ({exc}); retrying once")
        time.sleep(RETRY_DELAY_SECONDS)
        try:
            return self._request_once()
        except _TransientError as exc:
            raise SourceError(f"bing request failed twice: {exc}") from exc

    def _request_once(self):
        try:
            response = self.session.get(
                ARCHIVE_URL,
                params={"format": "js", "idx": 0, "n": 1, "mkt": "zh-CN"},
                headers={"User-Agent": BROWSER_USER_AGENT},
                timeout=(getattr(self.config, "connect_timeout", 5), REQUEST_TIMEOUT),
            )
        except requests.RequestException as exc:
            raise _TransientError(f"connection error: {exc}") from exc
        status = response.status_code
        if status == 429:
            # Retrying a rate limit only makes it worse; the source is parked
            # by the caller and comes back after SOURCE_RETRY_SECONDS.
            raise SourceError("bing rate limit hit (HTTP 429)")
        if status >= 400:
            detail = (
                f"HTTP {status}, content-type "
                f"{response.headers.get('Content-Type', 'unknown')}, "
                f"body {response.text[:120]!r}"
            )
            if status >= 500:
                raise _TransientError(detail)
            raise SourceError(f"bing HTTP {status}")
        try:
            return response.json()
        except ValueError as exc:
            raise _TransientError(
                f"malformed JSON (HTTP {status}, content-type "
                f"{response.headers.get('Content-Type', 'unknown')}, "
                f"body {response.text[:120]!r})"
            ) from exc

    def fetch(self, limit: int | None = None) -> list[Candidate]:
        payload = self._archive_payload()
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
