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


def test_banned_items_are_skipped(tmp_path):
    client = FakeClient([_item(banned=1, ban_reason="")])

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
    # Without return_tags Steam omits the tags array entirely, which would
    # silently disable the mature filter and the video detection.
    assert call["params"]["return_tags"] == "true"


def test_client_rejects_a_malformed_payload():
    session = _Session({"response": {}})

    with pytest.raises(SourceError):
        SteamWorkshopClient("KEY", session).query()


BROWSE_FIXTURE = """
<div class="card">
  <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=3799142774"
     class="thumb"><img
     src="https://images.steamusercontent.com/ugc/95/4DE/?ima=fit&amp;impolicy=Letterbox&amp;imw=322"
     alt="Rhine Lab · 莱茵生命"></a>
</div>
<div class="card">
  <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=3810650535"
     class="thumb"><img src="https://images.steamusercontent.com/ugc/15/060/"
     alt="FRAC_桌面互动壁纸"></a>
</div>
"""


def test_parse_browse_reads_ids_titles_and_previews():
    from dailywallpaper.sources.steam_workshop import parse_browse

    items = parse_browse(BROWSE_FIXTURE)

    assert [item["publishedfileid"] for item in items] == [
        "3799142774", "3810650535",
    ]
    assert items[0]["title"] == "Rhine Lab · 莱茵生命"
    # The resizing parameters are dropped: the original preview is downloaded.
    assert items[0]["preview_url"] == "https://images.steamusercontent.com/ugc/95/4DE/"


def test_the_source_falls_back_to_the_browse_page_when_the_api_dies():
    class _ApiDown:
        def query(self, **kwargs):
            raise SourceError("403 Forbidden")

        def browse(self, **kwargs):
            return [
                {
                    "publishedfileid": "1",
                    "title": "t",
                    "preview_url": "https://img/1.jpg",
                    "tags": [],
                    "file_size": 0,
                }
            ]

    source = SteamWorkshopSource(_ApiDown(), Path("root"))

    candidates = source.fetch()

    assert [candidate.source_id for candidate in candidates] == ["1"]
