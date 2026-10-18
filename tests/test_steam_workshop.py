# tests/test_steam_workshop.py
from pathlib import Path

import pytest

from dailywallpaper.sources.steam_workshop import (
    SteamWorkshopClient,
    SteamWorkshopSource,
    SourceError,
)


class FakeClient:
    def __init__(self, items):
        self.items = items
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.items


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _Response(self.payload)


def _item(**over):
    item = {
        "publishedfileid": "123",
        "title": "Neon",
        "preview_url": "https://cdn.example/preview.jpg",
        "file_size": 123456,
        "visibility": 0,
        "tags": [{"tag": "Scene"}, {"tag": "Anime"}],
        "subscriptions": 42,
    }
    item.update(over)
    return item


def test_items_become_candidates(tmp_path):
    client = FakeClient([_item()])

    items = SteamWorkshopSource(client, tmp_path).fetch()

    assert items[0].source == "steam_workshop"
    assert items[0].source_id == "123"
    assert items[0].title == "Neon"
    assert items[0].media_type == "scene"
    assert items[0].preview_url.endswith("preview.jpg")
    assert items[0].file_size == 123456
    # The project path is fixed before the download: readiness is file existence.
    assert items[0].file_url == str(tmp_path / "123" / "project.json")


def test_video_tag_maps_to_video(tmp_path):
    client = FakeClient([_item(tags=[{"tag": "Video"}])])

    assert SteamWorkshopSource(client, tmp_path).fetch()[0].media_type == "video"


def test_mature_items_are_skipped_unless_allowed(tmp_path):
    client = FakeClient([_item(tags=[{"tag": "Scene"}, {"tag": "Mature"}])])

    assert SteamWorkshopSource(client, tmp_path).fetch() == []
    assert len(SteamWorkshopSource(client, tmp_path, allow_mature=True).fetch()) == 1


def test_items_without_a_preview_are_skipped(tmp_path):
    client = FakeClient([_item(preview_url="")])

    assert SteamWorkshopSource(client, tmp_path).fetch() == []


def test_private_items_are_skipped(tmp_path):
    client = FakeClient([_item(visibility=1)])

    assert SteamWorkshopSource(client, tmp_path).fetch() == []


def test_query_asks_for_wallpaper_engine_items(tmp_path):
    client = FakeClient([])
    SteamWorkshopSource(client, tmp_path, query_type=9, days=7, per_page=5).fetch()

    call = client.calls[0]
    assert call["query_type"] == 9
    assert call["days"] == 7
    assert call["per_page"] == 5


def test_client_parses_publishedfiledetails():
    session = _Session({"response": {"publishedfiledetails": [_item()]}})

    details = SteamWorkshopClient("KEY", session).query()

    assert details[0]["publishedfileid"] == "123"
    call = session.calls[0]
    assert call["params"]["appid"] == 431960
    assert call["params"]["key"] == "KEY"


def test_client_rejects_a_malformed_payload():
    session = _Session({"response": {}})

    with pytest.raises(SourceError):
        SteamWorkshopClient("KEY", session).query()
