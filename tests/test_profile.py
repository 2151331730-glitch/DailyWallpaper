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

    # One 10 out of a library holding a single rating: the centre shrinks
    # toward the midpoint and the source mean shrinks toward neutral, so this
    # is above neutral but nowhere near the top of the range.
    assert profile.source_affinity("wallhaven") > 52.0
    assert profile.source_affinity("bing") == 50.0


def test_source_weight_is_a_shrunk_mean_of_its_ratings():
    """Three ratings must not speak as loudly as three hundred.

    The shrink is toward neutral, so the weight is the per-rating mean
    discounted by the evidence behind it -- not the raw sum the old code
    accumulated (which grew without bound as the library grew).
    """
    from dailywallpaper.recommend.profile import (
        SOURCE_PRIOR_STRENGTH,
        _rating_weight,
        rating_centre,
    )

    ratings = [10, 9, 2, 3, 7, 8]
    rows = [
        _row(["landscape"], rating, f"2026-09-{(index % 28) + 1:02d}T10:00:00")
        for index, rating in enumerate(ratings)
    ]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    centre = rating_centre(ratings)
    expected = sum(_rating_weight(rating, centre) for rating in ratings) / (
        len(ratings) + SOURCE_PRIOR_STRENGTH
    )
    assert profile.source_weights["wallhaven"] == pytest.approx(expected)
    # Discounted, so strictly nearer neutral than the plain mean would be.
    plain_mean = sum(_rating_weight(rating, centre) for rating in ratings) / len(ratings)
    assert abs(profile.source_weights["wallhaven"]) < abs(plain_mean)


def test_source_affinity_does_not_saturate_as_the_library_grows():
    """The bug this guards: a sum grew with the library and pinned the source.

    With the old sum-based weights, 300 ratings averaging +0.2 every day
    became raw +60, which clamped to affinity 100 -- and any library whose
    average leaned negative pinned every source to 0. Both destroyed the
    20% source term instead of expressing it.
    """
    rows = [
        _row(["landscape"], 7, f"2026-09-{(index % 28) + 1:02d}T10:00:00")
        for index in range(300)
    ]

    profile = PreferenceProfile.from_ratings(rows, NOW)

    affinity = profile.source_affinity("wallhaven")
    # A steady 7 is slightly above this library's own centre, so the source
    # should read slightly positive -- not 0, not 100.
    assert 50.0 < affinity < 70.0


def test_source_affinity_stays_meaningful_when_every_rating_is_low():
    """This library's real shape: mostly 1-4, with a few 7-9 favourites.

    With the old sum-based weights this library put the one source supplying
    97% of the candidates at affinity 0.0 -- a fixed 10-point penalty on every
    picture, no matter what the user thought. The mean keeps the term where it
    belongs: a small number that moves, instead of a clamp that cannot.
    """
    rows = (
        [_row(["landscape"], 3, "2026-09-01T10:00:00")]
        + [_row(["landscape"], 9, "2026-09-02T10:00:00")]
        + [_row(["landscape"], 3, "2026-09-03T10:00:00")]
        + [_row(["landscape"], 3, "2026-09-04T10:00:00")]
        + [_row(["landscape"], 4, "2026-09-05T10:00:00")]
        + [_row(["landscape"], 9, "2026-09-06T10:00:00")]
    )

    profile = PreferenceProfile.from_ratings(rows, NOW)

    affinity = profile.source_affinity("wallhaven")
    # Not pinned to either end of the 0..100 range the clamp used to produce.
    assert 10.0 < affinity < 90.0
    assert affinity != 0.0


def test_rating_centre_uses_the_users_own_average():
    from dailywallpaper.recommend.profile import rating_centre

    # A library that never goes above 6 must not treat 6 as "only average".
    assert rating_centre([4, 4, 5, 5, 6, 6] * 20) < 5.5

    # A tiny library is pulled back toward the midpoint, not trusted blindly.
    assert rating_centre([10]) < 9.0

    # Nothing to learn from yet.
    assert rating_centre([]) == 5.5


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
