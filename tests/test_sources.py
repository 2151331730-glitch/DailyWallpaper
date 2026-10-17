# tests/test_sources.py
import json
from pathlib import Path
import pytest
import requests
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
