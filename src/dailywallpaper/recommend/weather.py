# src/dailywallpaper/recommend/weather.py
"""Open-Meteo weather lookup and WMO-code to mood-tag mapping (spec 8.8)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


def cached_weather(
    session, conn, latitude: float, longitude: float, cache_minutes: int, now=None
) -> WeatherNow:
    """Return weather for a coordinate pair, reusing a recent cached reading.

    The reading lives in the ``state`` table so a restart inside the cache
    window still avoids a network call.
    """
    moment = now or datetime.now(timezone.utc)
    key = f"weather_{latitude}_{longitude}"
    row = conn.execute("select value from state where key=?", (key,)).fetchone()
    if row:
        try:
            data = json.loads(row["value"])
            fetched_at = datetime.fromisoformat(data["fetched_at"])
            if moment - fetched_at < timedelta(minutes=cache_minutes):
                return WeatherNow(
                    temperature=float(data["temperature"]),
                    code=int(data["code"]),
                    fetched_at=fetched_at,
                )
        except (ValueError, KeyError, TypeError):
            pass
    weather = fetch_weather(session, latitude, longitude)
    conn.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            key,
            json.dumps(
                {
                    "temperature": weather.temperature,
                    "code": weather.code,
                    "fetched_at": weather.fetched_at.isoformat(),
                }
            ),
        ),
    )
    conn.commit()
    return weather


_WEATHER_LABELS = (
    ((0, 1), "晴"),
    ((2, 3), "多云"),
    ((45, 48), "雾"),
    ((51, 67), "雨"),
    ((71, 77), "雪"),
    ((80, 82), "阵雨"),
    ((85, 86), "阵雪"),
    ((95, 99), "雷雨"),
)


def weather_label(code: int) -> str:
    """Short Chinese label for the recommendation reason."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return ""
    for (low, high), label in _WEATHER_LABELS:
        if low <= value <= high:
            return label
    return ""
