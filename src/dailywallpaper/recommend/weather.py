# src/dailywallpaper/recommend/weather.py
"""Open-Meteo weather lookup and WMO-code to mood-tag mapping (spec 8.8)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import requests

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEZONE = "Asia/Shanghai"
REQUEST_TIMEOUT = 30


class WeatherError(RuntimeError):
    """Raised when Open-Meteo returns unusable data."""


@dataclass(frozen=True)
class WeatherNow:
    temperature: float
    code: int
    fetched_at: datetime


# (inclusive WMO code range, mood tags) — spec section 8.8.
_WEATHER_TAG_RULES: tuple[tuple[tuple[int, int], frozenset[str]], ...] = (
    ((0, 1), frozenset({"bright", "vibrant", "blue", "warm"})),
    ((2, 3), frozenset({"soft", "muted", "calm"})),
    ((45, 48), frozenset({"misty", "minimal", "cool"})),
    ((51, 67), frozenset({"cozy", "moody", "rain", "indoor"})),
    ((71, 77), frozenset({"cold", "white", "winter", "calm"})),
    ((80, 82), frozenset({"cozy", "moody", "rain", "indoor"})),
    ((85, 86), frozenset({"cold", "white", "winter", "calm"})),
    ((95, 99), frozenset({"dramatic", "dark", "storm"})),
)


def weather_tags(code: int) -> set[str]:
    """Map a WMO weather code to its mood tags; unknown codes map to empty."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return set()
    for (low, high), tags in _WEATHER_TAG_RULES:
        if low <= value <= high:
            return set(tags)
    return set()


def weather_fit(mood_tags: Iterable[str] | None, code: int) -> int:
    """Score how well candidate mood tags fit the current weather.

    Returns 100 when the tags intersect the weather mood tags, otherwise 50.
    """
    normalized = {
        tag.strip().lower()
        for tag in (mood_tags or ())
        if isinstance(tag, str) and tag.strip()
    }
    return 100 if normalized & weather_tags(code) else 50


def fetch_weather(session: requests.Session, latitude: float, longitude: float) -> WeatherNow:
    """Fetch current temperature and WMO code for a coordinate pair."""
    response = session.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,weather_code",
            "timezone": DEFAULT_TIMEZONE,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    try:
        payload = response.json()
        current = payload["current"]
        temperature = float(current["temperature_2m"])
        code = int(current["weather_code"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WeatherError("open-meteo returned malformed payload") from exc
    return WeatherNow(
        temperature=temperature,
        code=code,
        fetched_at=datetime.now(timezone.utc),
    )
