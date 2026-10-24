# tests/test_weather.py
import pytest
import requests
from datetime import timedelta

from dailywallpaper.db import connect, migrate
from dailywallpaper.recommend.weather import (
    WeatherError,
    cached_weather,
    fetch_weather,
    weather_fit,
    weather_tags,
)

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

@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (0, {"bright", "vibrant", "blue", "warm"}),
        (1, {"bright", "vibrant", "blue", "warm"}),
        (2, {"soft", "muted", "calm"}),
        (3, {"soft", "muted", "calm"}),
        (45, {"misty", "minimal", "cool"}),
        (48, {"misty", "minimal", "cool"}),
        (51, {"cozy", "moody", "rain", "indoor"}),
        (61, {"cozy", "moody", "rain", "indoor"}),
        (67, {"cozy", "moody", "rain", "indoor"}),
        (80, {"cozy", "moody", "rain", "indoor"}),
        (82, {"cozy", "moody", "rain", "indoor"}),
        (71, {"cold", "white", "winter", "calm"}),
        (77, {"cold", "white", "winter", "calm"}),
        (85, {"cold", "white", "winter", "calm"}),
        (86, {"cold", "white", "winter", "calm"}),
        (95, {"dramatic", "dark", "storm"}),
        (99, {"dramatic", "dark", "storm"}),
    ],
)
def test_weather_tags_mapping(code, expected):
    assert weather_tags(code) == expected

def test_weather_tags_unknown_code_is_empty():
    assert weather_tags(123) == set()

def test_weather_tags_are_immutable_and_cached():
    tags = weather_tags(0)
    assert isinstance(tags, frozenset)
    with pytest.raises(AttributeError):
        tags.add("mutated")
    assert weather_tags(0) == tags

def test_weather_fit_intersection_returns_100():
    assert weather_fit(["moody", "minimal"], 61) == 100
    assert weather_fit(["Bright"], 0) == 100

def test_weather_fit_no_intersection_returns_50():
    assert weather_fit(["moody"], 0) == 50
    assert weather_fit([], 0) == 50
    assert weather_fit(["moody"], 123) == 50

def test_fetch_weather_parses_current():
    payload = {
        "latitude": 39.9,
        "longitude": 116.4,
        "timezone": "Asia/Shanghai",
        "current": {
            "time": "2026-10-17T12:00",
            "temperature_2m": 21.5,
            "weather_code": 3,
        },
    }
    weather = fetch_weather(FakeSession(payload), 39.9075, 116.39723)
    assert weather.temperature == 21.5
    assert weather.code == 3
    assert weather.fetched_at.tzinfo is not None

def test_fetch_weather_http_error_raises():
    with pytest.raises(requests.HTTPError):
        fetch_weather(FakeSession({}, status=500), 0.0, 0.0)

def test_fetch_weather_malformed_payload_raises_weather_error():
    with pytest.raises(WeatherError):
        fetch_weather(FakeSession({"current": {}}), 0.0, 0.0)


def _current_payload(temperature=21.5, code=3):
    return {"current": {"temperature_2m": temperature, "weather_code": code}}


def test_cached_weather_reuses_a_recent_reading(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    first = cached_weather(FakeSession(_current_payload()), conn, 39.9, 116.4, 60)

    class Boom:
        def get(self, *args, **kwargs):
            raise AssertionError("cache should have answered")

    second = cached_weather(
        Boom(), conn, 39.9, 116.4, 60, now=first.fetched_at + timedelta(minutes=30)
    )

    assert second.temperature == 21.5
    assert second.code == 3


def test_cached_weather_refetches_after_the_window(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    first = cached_weather(FakeSession(_current_payload()), conn, 39.9, 116.4, 60)

    calls = []

    class S:
        def get(self, *args, **kwargs):
            calls.append(1)
            return FakeResponse(_current_payload(5.0, 71))

    second = cached_weather(
        S(), conn, 39.9, 116.4, 60, now=first.fetched_at + timedelta(minutes=120)
    )

    assert calls == [1]
    assert second.temperature == 5.0
