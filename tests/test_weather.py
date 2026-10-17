# tests/test_weather.py
import pytest
import requests
from dailywallpaper.recommend.weather import (
    WeatherError,
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

def test_weather_tags_returns_independent_sets():
    tags = weather_tags(0)
    tags.add("mutated")
    assert "mutated" not in weather_tags(0)

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
