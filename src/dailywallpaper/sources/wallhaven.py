# src/dailywallpaper/sources/wallhaven.py
"""Wallhaven search adapter (SFW, no API key)."""
from __future__ import annotations

import time

import requests

from dailywallpaper.config import SourcesSection

from .base import Candidate, SourceError

SEARCH_URL = "https://wallhaven.cc/api/v1/search"
USER_AGENT = "DailyWallpaper/0.1 (local app)"
PAGE_SIZE = 24
REQUEST_TIMEOUT = 30
REQUEST_SPACING_SECONDS = 1.0
# Spec 4.1: mix sorts so the pool is not only popular/template images.
SORT_PLAN = ("toplist", "toplist", "random", "date_added")


class WallhavenSource:
    """Fetch wallpaper metadata from Wallhaven's public search API.

    Only metadata is requested; image and thumbnail bytes are never fetched
    here. Requests are spaced at least one second apart and the number of pages
    is bounded by ``SourcesSection.wallhaven_pages``.
    """

    def __init__(self, session: requests.Session, config: SourcesSection | None = None):
        self.session = session
        self.config = config or SourcesSection()
        self._requests_made = 0

    def fetch(self, limit: int = PAGE_SIZE, query: str | None = None) -> list[Candidate]:
        if limit <= 0:
            return []
        pages: list[list[Candidate]] = []
        for page in range(1, self.config.wallhaven_pages + 1):
            payload = self._search(page, query=query)
            data = payload.get("data")
            if not isinstance(data, list):
                raise SourceError("wallhaven response is missing the data list")
            pages.append([_to_candidate(item) for item in data])
            # A short page means the result set is exhausted.
            if len(data) < PAGE_SIZE:
                break
        # Interleave the pages so a small limit still spans every sort type.
        # Concatenating would let the two toplist pages fill the whole cap and
        # leave the random/date_added diversity dead at the default settings.
        merged: list[Candidate] = []
        for index in range(max((len(page_items) for page_items in pages), default=0)):
            for page_items in pages:
                if index < len(page_items):
                    merged.append(page_items[index])
        return merged[:limit]

    def _sort_for_page(self, page: int) -> tuple[str, str | None]:
        """Return (sorting, topRange) for a 1-based page number."""
        sorting = SORT_PLAN[(page - 1) % len(SORT_PLAN)]
        return sorting, ("1M" if sorting == "toplist" else None)

    def _search(self, page: int, query: str | None = None) -> dict:
        if self._requests_made:
            time.sleep(REQUEST_SPACING_SECONDS)
        self._requests_made += 1
        sorting, top_range = self._sort_for_page(page)
        params = {
            "purity": "100",
            "categories": self.config.wallhaven_categories,
            "atleast": f"{self.config.min_width}x{self.config.min_height}",
            "ratios": "16x9",
            "sorting": sorting,
            "page": page,
        }
        if query:
            # Wallhaven's q is an English keyword search; it ignores Chinese.
            params["q"] = query
        if top_range:
            params["topRange"] = top_range
        try:
            response = self.session.get(
                SEARCH_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=(self.config.connect_timeout, REQUEST_TIMEOUT),
            )
        except requests.RequestException as exc:
            raise SourceError(f"wallhaven request failed: {exc}") from exc
        if response.status_code == 429:
            raise SourceError("wallhaven rate limit hit (HTTP 429)")
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise SourceError(f"wallhaven HTTP {response.status_code}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("wallhaven returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise SourceError("wallhaven response is not a JSON object")
        return payload


def _to_candidate(item: dict) -> Candidate:
    try:
        thumbs = item["thumbs"]
        return Candidate(
            source="wallhaven",
            source_id=str(item["id"]),
            title=str(item.get("id") or ""),
            page_url=str(item.get("url") or ""),
            file_url=str(item["path"]),
            preview_url=str(thumbs["large"]),
            media_type="image",
            width=int(item.get("dimension_x") or 0),
            height=int(item.get("dimension_y") or 0),
            file_size=int(item.get("file_size") or 0),
            tags=[item["category"]] if item.get("category") else [],
            raw=item,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError("wallhaven item is missing required fields") from exc
