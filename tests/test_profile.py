# tests/test_profile.py
import json
from datetime import datetime

import pytest

from dailywallpaper.recommend.profile import PreferenceProfile

NOW = datetime.fromisoformat("2026-10-18T10:00:00")


def _row(tags, rating, created_at, source="wallhaven"):
    return {
        "derived_tags_json": json.dumps(tags),
        "rating": rating,
        "created_at": created_at,
        "source": source,
    }


def test_liked_tag_outranks_disliked_tag():
    rows = [
        _row(["landscape"], 10, "2026-10-01T10:00:00"),
        _row(["anime"], 1, "2026-10-01T10:00:00"),
    ]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    assert profile.tag_affinity(["landscape"]) > profile.tag_affinity(["anime"])


def test_profile_learns_from_analysis_features_not_the_single_tag():
    rows = [
        {
            "style_tags_json": json.dumps(["fog", "muted-palette"]),
            "mood_tags_json": json.dumps(["calm"]),
            "derived_tags_json": json.dumps(["landscape"]),
            "rating": 10,
            "created_at": "2026-10-01T10:00:00",
            "source": "wallhaven",
        }
    ]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    assert profile.tag_affinity(["fog"]) > 50
    assert profile.tag_affinity(["mood", "calm"]) > 50
    assert profile.tag_affinity(["landscape"]) == 50


def test_profile_falls_back_to_the_stored_tag_without_an_analysis():
    rows = [_row(["landscape"], 10, "2026-10-01T10:00:00")]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    assert profile.tag_affinity(["landscape"]) > 50


def test_rating_older_than_180_days_has_quarter_weight():
    rows = [
        _row(["landscape"], 10, "2026-02-01T10:00:00"),  # ~259 days
        _row(["anime"], 10, "2026-10-01T10:00:00"),      # 17 days
    ]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    ratio = profile.tag_weights["landscape"] / profile.tag_weights["anime"]
    assert ratio == pytest.approx(0.25)


def test_rating_between_90_and_180_days_has_half_weight():
    rows = [
        _row(["landscape"], 10, "2026-06-01T10:00:00"),  # ~139 days
        _row(["anime"], 10, "2026-10-01T10:00:00"),
    ]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    ratio = profile.tag_weights["landscape"] / profile.tag_weights["anime"]
    assert ratio == pytest.approx(0.5)


def test_empty_profile_is_neutral():
    profile = PreferenceProfile.from_ratings([], NOW)

    assert profile.tag_affinity(["landscape"]) == 50.0
    assert profile.source_affinity("wallhaven") == 50.0
    assert profile.rating_count == 0


def test_source_affinity_is_high_for_liked_source():
    rows = [_row(["landscape"], 10, "2026-10-01T10:00:00", source="wallhaven")]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    assert profile.source_affinity("wallhaven") == 100.0
    assert profile.source_affinity("bing") == 50.0


def test_rows_without_a_source_key_are_tolerated():
    rows = [
        {
            "derived_tags_json": json.dumps(["landscape"]),
            "rating": 10,
            "created_at": "2026-10-01T10:00:00",
        }
    ]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    assert profile.tag_weights["landscape"] > 0
    assert profile.source_weights == {}


def test_aware_now_with_naive_created_at_does_not_crash():
    from datetime import timezone

    aware = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    profile = PreferenceProfile.from_ratings(
        [_row(["landscape"], 10, "2026-10-01T10:00:00")], aware
    )

    assert profile.rating_count == 1


def test_rating_count_counts_rows():
    rows = [
        _row(["landscape"], 10, "2026-10-01T10:00:00"),
        _row(["anime"], 1, "2026-10-02T10:00:00"),
    ]

    assert PreferenceProfile.from_ratings(rows, NOW).rating_count == 2


def test_banned_ratings_are_strong_negative_signals():
    """A 1-2 is not skipped: the profile must learn the style to avoid it."""
    rows = [
        _row(["landscape"], 10, "2026-10-01T10:00:00"),
        _row(["anime"], 1, "2026-10-02T10:00:00"),
        _row(["sci-fi"], 2, "2026-10-03T10:00:00"),
    ]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    assert profile.tag_weights["anime"] < 0
    assert profile.tag_weights["sci-fi"] < 0
    # The 1 is pushed past the linear -1, so it outweighs a plain 2.
    assert profile.tag_weights["anime"] < profile.tag_weights["sci-fi"]
