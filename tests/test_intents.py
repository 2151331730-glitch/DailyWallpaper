from datetime import datetime
from types import SimpleNamespace

from dailywallpaper.app import RealPipeline
from dailywallpaper.db import connect, migrate
from dailywallpaper.recommend import intents


def _db(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn


def _search_intent(**overrides):
    base = {
        "kind": "search",
        "polarity": 1,
        "tags": [],
        "query_zh": "C罗",
        "query_en": "cristiano ronaldo",
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def test_validate_search_requires_a_query():
    assert intents._validate({"kind": "search", "tags": []}) is None


def test_validate_normalises_tags_and_polarity():
    intent = intents._validate(
        {"kind": "preference", "polarity": -1, "tags": ["Anime", "Neon"],
         "confidence": 0.9}
    )

    assert intent["kind"] == "preference"
    assert intent["polarity"] == -1
    assert intent["tags"] == ["anime", "neon"]


def test_validate_rejects_unknown_kinds():
    assert intents._validate({"kind": "nonsense", "tags": []}) is None


def test_store_list_and_remove(tmp_path):
    conn = _db(tmp_path)
    intent_id = intents.store(conn, _search_intent(), None, "想看C罗")

    listed = intents.list_intents(conn)
    assert listed[0]["id"] == intent_id
    assert intents.search_queries(conn)[0]["query_zh"] == "C罗"

    intents.remove_intent(conn, intent_id)
    assert intents.list_intents(conn) == []


def test_preference_terms_split_by_polarity(tmp_path):
    conn = _db(tmp_path)
    intents.store(
        conn,
        {"kind": "preference", "polarity": 1, "tags": ["surreal"],
         "query_zh": "", "query_en": "", "confidence": 0.9},
        None, "喜欢超现实",
    )
    intents.store(
        conn,
        {"kind": "preference", "polarity": -1, "tags": ["anime"],
         "query_zh": "", "query_en": "", "confidence": 0.9},
        None, "不要动漫",
    )

    boost, avoid = intents.preference_terms(conn)

    assert "surreal" in boost
    assert "anime" in avoid


def test_search_intents_go_to_sources_that_can_search(tmp_path):
    conn = _db(tmp_path)
    intents.store(conn, _search_intent(), None, "想看C罗")
    pipeline = RealPipeline.__new__(RealPipeline)
    pipeline.conn = conn
    pipeline.cfg = SimpleNamespace(app=SimpleNamespace(max_analyses_per_day=40))
    calls = []

    class SteamWorkshopSource:
        def fetch(self, limit=None, query=None):
            calls.append(("steam", query, limit))
            return ["steam-item"]

    class WallhavenSource:
        def fetch(self, limit=None, query=None, max_pages=None):
            calls.append(("wallhaven", query, limit, max_pages))
            return ["wallhaven-item"]

    class BingSource:
        def fetch(self, limit=None):
            calls.append(("bing", None, None))
            return ["bing-item"]

    pipeline.sources = [SteamWorkshopSource(), WallhavenSource(), BingSource()]

    found = pipeline._fetch_search_candidates(
        datetime(2026, 10, 22, 9, 0), "2026-10-22"
    )

    # One search takes half the 40-analysis budget; Wallhaven only needs one
    # page (max_pages=1), Steam keeps its usual page size.
    assert ("steam", "C罗", 20) in calls
    assert ("wallhaven", "cristiano ronaldo", 20, 1) in calls
    assert all(call[0] != "bing" for call in calls)
    assert len(found) == 2


def test_validate_tolerates_string_numbers():
    intent = intents._validate(
        {"kind": "preference", "polarity": "high", "tags": ["Fog"],
         "confidence": "0.8"}
    )

    assert intent["polarity"] == 1
    assert intent["confidence"] == 0.8


def test_the_same_search_is_stored_once(tmp_path):
    conn = _db(tmp_path)

    first = intents.store(conn, _search_intent(), None, "想看C罗")
    second = intents.store(conn, _search_intent(), None, "想看C罗")

    assert first == second
    assert len(intents.list_intents(conn)) == 1
