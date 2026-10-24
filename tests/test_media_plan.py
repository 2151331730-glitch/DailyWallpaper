# tests/test_media_plan.py
from datetime import date, timedelta

import pytest

from dailywallpaper.recommend.media_plan import (
    DYNAMIC,
    STATIC,
    MediaPlan,
    dynamic_days,
    dynamic_streak,
)

DAY = date(2026, 10, 18)


def choose(**overrides):
    """Ask the strategy with a plausible default, then override one thing."""
    values = dict(
        day=DAY,
        strategy="mixed",
        best_dynamic=80.0,
        best_static=70.0,
        on_battery=False,
        dynamic_available=True,
        history=(),
        margin=0.0,
        max_streak=2,
        quota_days=10,
        quota_max=5,
    )
    values.update(overrides)
    return MediaPlan.choose(**values)


def test_static_only_never_takes_dynamic_even_when_it_scores_higher():
    assert choose(strategy="static_only", best_dynamic=99, best_static=10).kind == STATIC


def test_static_only_falls_back_to_dynamic_when_nothing_static_qualified():
    choice = choose(strategy="static_only", best_dynamic=80, best_static=None)

    assert choice.kind == DYNAMIC


def test_static_only_never_falls_back_on_battery():
    choice = choose(
        strategy="static_only", best_dynamic=80, best_static=None, on_battery=True
    )

    assert choice.kind == STATIC


def test_dynamic_only_takes_dynamic_when_available():
    assert choose(strategy="dynamic_only").kind == DYNAMIC


def test_dynamic_only_falls_back_to_static_when_no_dynamic_qualified():
    choice = choose(strategy="dynamic_only", best_dynamic=None, best_static=70)

    assert choice.kind == STATIC


def test_battery_forces_static():
    assert choose(on_battery=True).kind == STATIC


def test_mixed_takes_dynamic_when_strictly_higher():
    assert choose(best_dynamic=80.1, best_static=80.0).kind == DYNAMIC


def test_mixed_keeps_static_on_a_tie():
    assert choose(best_dynamic=80.0, best_static=80.0).kind == STATIC


def test_mixed_respects_the_margin():
    assert choose(best_dynamic=84.9, best_static=80.0, margin=5).kind == STATIC
    assert choose(best_dynamic=85.1, best_static=80.0, margin=5).kind == DYNAMIC


def test_mixed_without_dynamic_candidates_stays_static():
    assert choose(best_dynamic=None, dynamic_available=False).kind == STATIC


def test_mixed_without_static_candidates_takes_dynamic():
    assert choose(best_static=None).kind == DYNAMIC


def test_a_dynamic_streak_of_two_days_stops_a_third():
    history = [
        ((DAY - timedelta(days=2)).isoformat(), DYNAMIC),
        ((DAY - timedelta(days=1)).isoformat(), DYNAMIC),
    ]

    assert choose(history=history).kind == STATIC


def test_a_static_day_resets_the_dynamic_streak():
    history = [
        ((DAY - timedelta(days=3)).isoformat(), DYNAMIC),
        ((DAY - timedelta(days=2)).isoformat(), DYNAMIC),
        ((DAY - timedelta(days=1)).isoformat(), STATIC),
    ]

    assert choose(history=history).kind == DYNAMIC


def test_five_dynamic_days_in_the_last_ten_stop_a_sixth():
    history = [
        ((DAY - timedelta(days=offset)).isoformat(), DYNAMIC)
        for offset in (10, 9, 8, 7, 6)
    ]

    assert dynamic_days(history, DAY, 10) == 5
    assert choose(history=history).kind == STATIC


def test_days_outside_the_quota_window_do_not_count():
    history = [
        ((DAY - timedelta(days=offset)).isoformat(), DYNAMIC)
        for offset in (15, 14, 13, 12, 11)
    ]

    assert dynamic_days(history, DAY, 10) == 0
    assert choose(history=history).kind == DYNAMIC


def test_the_quota_window_excludes_today():
    history = [(DAY.isoformat(), DYNAMIC)]

    assert dynamic_days(history, DAY, 10) == 0


def test_streak_requires_consecutive_days():
    history = [
        ((DAY - timedelta(days=3)).isoformat(), DYNAMIC),
        ((DAY - timedelta(days=1)).isoformat(), DYNAMIC),
    ]

    assert dynamic_streak(history, DAY) == 1


def test_streak_ignores_a_missing_yesterday():
    history = [((DAY - timedelta(days=2)).isoformat(), DYNAMIC)]

    assert dynamic_streak(history, DAY) == 0


@pytest.mark.parametrize("strategy", ["whatever", "", "STATIC"])
def test_an_unknown_strategy_behaves_like_static_only(strategy):
    assert choose(strategy=strategy).kind == STATIC
