# tests/test_media_plan.py
from datetime import date

import pytest

from dailywallpaper.recommend.media_plan import MediaPlan


@pytest.mark.parametrize(
    ("strategy", "expected"), [("static_only", "static"), ("dynamic_only", "dynamic")]
)
def test_explicit_strategies(strategy, expected):
    assert MediaPlan.decide(date(2026, 10, 18), strategy, 3, False) == expected


def test_mixed_schedules_one_dynamic_day_every_n():
    days = [date(2026, 10, 1 + index) for index in range(6)]

    kinds = [MediaPlan.decide(day, "mixed", 3, False) for day in days]

    assert kinds.count("dynamic") == 2


def test_mixed_is_off_for_n_of_one_or_less():
    assert MediaPlan.decide(date(2026, 10, 18), "mixed", 1, False) == "static"
    assert MediaPlan.decide(date(2026, 10, 18), "mixed", 0, False) == "static"


def test_battery_forces_static():
    assert MediaPlan.decide(date(2026, 10, 18), "mixed", 1, True) == "static"


def test_dynamic_only_on_battery_falls_back_to_static():
    assert MediaPlan.decide(date(2026, 10, 18), "dynamic_only", 3, True) == "static"


def test_unknown_strategy_beats_static():
    assert MediaPlan.decide(date(2026, 10, 18), "whatever", 3, False) == "static"
