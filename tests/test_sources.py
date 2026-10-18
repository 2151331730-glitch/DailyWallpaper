# tests/test_sources.py
import json
from pathlib import Path
import pytest
import requests
from dailywallpaper.config import SourcesSection
from dailywallpaper.db import connect, migrate
from dailywallpaper.sources.base import Candidate, SourceError
from dailywallpaper.sources.repository import fetch_all, upsert_wallpaper
from dailywallpaper.sources.wallhaven import WallhavenSource
from dailywallpaper.sources.bing import BingSource

class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))
    def json(self):
        return self.payload

class FakeSession:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
    def get(self, *args, **kwargs):
        return FakeResponse(self.payload, self.status)

def test_wallhaven_parses_fixture():
    payload = json.loads(Path("tests/fixtures/wallhaven.json").read_text(encoding="utf-8"))
    items = WallhavenSource(FakeSession(payload)).fetch(24)
    assert items[0].source_id == "jel1jq"
    assert items[0].width == 3840
    assert items[0].preview_url.endswith("jel1jq.jpg")

def test_bing_parses_fixture():
    payload = json.loads(Path("tests/fixtures/bing.json").read_text(encoding="utf-8"))
    items = BingSource(FakeSession(payload)).fetch()
    assert items[0].file_url.endswith("_UHD.jpg")
    assert items[0].source == "bing"

def test_429_raises_source_error():
    with pytest.raises(Exception):
        WallhavenSource(FakeSession({}, status=429)).fetch(24)


def test_upsert_dedupes(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    c = Candidate("wallhaven", "abc", "T", "p", "f", "v", "image", 2560, 1440, 1000, [], {})
    a = upsert_wallpaper(conn, c)
    b = upsert_wallpaper(conn, c)
    assert a == b
    assert conn.execute("select count(*) from wallpapers").fetchone()[0] == 1


def test_upsert_updates_metadata_on_conflict(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    c = Candidate("wallhaven", "abc", "old", "p", "f", "v", "image", 2560, 1440, 1000, [], {})
    upsert_wallpaper(conn, c)
    newer = Candidate("wallhaven", "abc", "new", "p", "f", "v", "image", 2560, 1440, 1000, [], {})
    upsert_wallpaper(conn, newer)
    assert conn.execute("select title from wallpapers").fetchone()[0] == "new"


def test_wallhaven_sort_plan():
    src = WallhavenSource(FakeSession({}))
    assert [src._sort_for_page(p) for p in (1, 2, 3, 4)] == [
        ("toplist", "1M"), ("toplist", "1M"), ("random", None), ("date_added", None),
    ]


def test_wallhaven_sends_toprange_only_for_toplist():
    calls = []

    class S:
        def get(self, url, params, headers, timeout):
            calls.append(dict(params))
            return FakeResponse({"data": []})

    src = WallhavenSource(S())
    src._search(1)
    src._search(3)
    assert calls[0]["topRange"] == "1M"
    assert calls[0]["sorting"] == "toplist"
    assert "topRange" not in calls[1]
    assert calls[1]["sorting"] == "random"


def test_wallhaven_malformed_payload_raises_source_error():
    with pytest.raises(SourceError):
        WallhavenSource(FakeSession(["not", "a", "dict"])).fetch(24)


def _wh_item(page, index):
    return {
        "id": f"p{page}-{index}",
        "url": "u",
        "path": "f",
        "thumbs": {"large": "v"},
        "dimension_x": 3840,
        "dimension_y": 2160,
        "file_size": 1000,
        "category": "general",
    }


def test_wallhaven_fetches_every_configured_page_and_mixes_sorts():
    calls = []

    class S:
        def get(self, url, params, headers, timeout):
            calls.append(dict(params))
            page = int(params["page"])
            return FakeResponse({"data": [_wh_item(page, i) for i in range(24)]})

    src = WallhavenSource(S(), SourcesSection(wallhaven_pages=4))
    items = src.fetch(40)

    assert [c["sorting"] for c in calls] == ["toplist", "toplist", "random", "date_added"]
    assert len(items) == 40
    # Pages are interleaved, so a small limit still spans every sort type.
    assert [item.source_id for item in items[:4]] == ["p1-0", "p2-0", "p3-0", "p4-0"]


def test_fetch_all_continues_when_one_source_raises():
    class Boom:
        def fetch(self, limit=None):
            raise SourceError("boom")

    class Ok:
        def fetch(self, limit=None):
            return [Candidate("t", "1", "", "", "", "", "image", 0, 0, 0, [], {})]

    items = fetch_all([Boom(), Ok()], 10)
    assert len(items) == 1


def test_source_is_not_retried_after_a_failure_today(tmp_path):
    from dailywallpaper.sources.repository import mark_source_down, source_is_down

    conn = connect(tmp_path / "app.db")
    migrate(conn)

    assert source_is_down(conn, "WallhavenSource", "2026-10-18") is False
    mark_source_down(conn, "WallhavenSource", "2026-10-18")
    assert source_is_down(conn, "WallhavenSource", "2026-10-18") is True
    assert source_is_down(conn, "WallhavenSource", "2026-10-19") is False


def test_wallhaven_uses_the_short_connect_timeout():
    seen = {}

    class S:
        def get(self, url, params, headers, timeout):
            seen["timeout"] = timeout
            return FakeResponse({"data": []})

    WallhavenSource(S(), SourcesSection(connect_timeout=5))._search(1)

    assert seen["timeout"] == (5, 30)


def test_a_source_recovers_after_the_retry_window(tmp_path):
    import time as _time

    from dailywallpaper.db import connect, migrate
    from dailywallpaper.sources.repository import mark_source_down, source_is_down

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    mark_source_down(conn, "WallhavenSource", "2026-10-18")
    conn.execute(
        "update state set value=? where key='source_down_WallhavenSource_2026-10-18'",
        (str(_time.time() - 1801),),
    )
    conn.commit()

    assert source_is_down(conn, "WallhavenSource", "2026-10-18") is False
