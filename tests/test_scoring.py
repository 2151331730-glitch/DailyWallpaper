# tests/test_scoring.py
from pathlib import Path

import pytest

from dailywallpaper.analysis.scoring import (
    CONTEXT_WEIGHT,
    EXPLORE_WEIGHT,
    FORMULAIC_PENALTY_CAP,
    PERSONALIZE_WEIGHT,
    SUGAR_PENALTY_MAX,
    context_adjust,
    explore_score,
    final_score,
    hard_gate,
    rank,
    sugar_penalty,
)
from dailywallpaper.analysis.vision import VisionScores
from dailywallpaper.models import Decision


def _scores(**overrides):
    base = dict(
        quality=80,
        creativity=70,
        novelty=60,
        formulaic=20,
        nsfw=False,
        watermark=False,
        text_overlay="none",
        style_tags=["landscape"],
        mood_tags=["calm"],
        palette=["#112233"],
        subject="s",
        reason="r",
    )
    base.update(overrides)
    return VisionScores(**base)


class _Profile:
    def __init__(self, n=0, tag=100.0, source=100.0):
        self.rating_count = n
        self._tag = tag
        self._source = source

    def tag_affinity(self, tags):
        return self._tag

    def source_affinity(self, source):
        return self._source


def _decision(wid, final, quality=80):
    return Decision(
        wallpaper_id=wid,
        image_path=Path("x.jpg"),
        scores=_scores(quality=quality),
        explore=0.0,
        personalize=0.0,
        final=final,
        reason="r",
    )


def test_final_is_the_configured_mix_of_explore_and_personalize():
    result = final_score(_scores(), "wallhaven", _Profile(n=10), [], 0)

    assert result.final == pytest.approx(
        EXPLORE_WEIGHT * result.explore + PERSONALIZE_WEIGHT * result.personalize
    )


def test_a_formulaic_image_is_ranked_down_not_just_gated():
    original = final_score(_scores(formulaic=20), "wallhaven", _Profile(n=10), [], 0)
    cliche = final_score(_scores(formulaic=60), "wallhaven", _Profile(n=10), [], 0)

    assert cliche.final < original.final
    # Below the ceiling the penalty is linear; 60 is already past it.
    assert original.final - cliche.final == pytest.approx(FORMULAIC_PENALTY_CAP)


def test_the_formulaic_penalty_has_a_ceiling():
    original = final_score(_scores(formulaic=20), "wallhaven", _Profile(n=10), [], 0)
    mild = final_score(_scores(formulaic=24), "wallhaven", _Profile(n=10), [], 0)
    extreme = final_score(_scores(formulaic=100), "wallhaven", _Profile(n=10), [], 0)

    # Under the cap the penalty stays proportional: 4 points over the floor.
    assert original.final - mild.final == pytest.approx(0.5 * 4)
    # Past it, a worse picture is not punished any further. creativity and
    # novelty already carry 70% of the explore term on the same axis
    # (correlation -0.80 / -0.90 with formulaic), so an unbounded penalty
    # counted one signal twice and could outweigh every other adjustment.
    assert original.final - extreme.final == pytest.approx(FORMULAIC_PENALTY_CAP)


def test_sugar_penalty_only_docks_above_neutral():
    assert sugar_penalty(None) == 0.0
    assert sugar_penalty(50) == 0.0
    assert sugar_penalty(30) == 0.0
    assert sugar_penalty(100) == pytest.approx(SUGAR_PENALTY_MAX)


def test_a_sugar_score_replaces_the_formulaic_penalty():
    neutral_sugar = final_score(_scores(formulaic=100), "wallhaven",
                                _Profile(n=10), [], 0, sugar=50)
    sugary = final_score(_scores(formulaic=100), "wallhaven",
                         _Profile(n=10), [], 0, sugar=100)

    assert neutral_sugar.final - sugary.final == pytest.approx(SUGAR_PENALTY_MAX)
    # With a model present the formulaic score must not also be applied:
    # otherwise a picture would be charged twice for one thing.
    plain_formulaic = final_score(_scores(formulaic=20), "wallhaven",
                                  _Profile(n=10), [], 0, sugar=50)
    assert plain_formulaic.final == pytest.approx(neutral_sugar.final)


def test_personalize_reads_mood_as_well_as_style():
    seen = []

    class _Recording(_Profile):
        def tag_affinity(self, tags):
            seen.extend(tags)
            return 50.0

    final_score(_scores(), "wallhaven", _Recording(n=10), [], 0)

    assert seen == ["landscape", "calm"]


def test_cold_start_personalize_is_60():
    result = final_score(_scores(), "wallhaven", _Profile(n=0), [], 0)

    assert result.personalize == 60.0


def test_warm_profile_pulls_personalize_toward_affinity():
    # p = 0.7*100 + 0.2*100 + 0.1*50 = 95; n=10 -> (10/15)*95 + (5/15)*60
    result = final_score(_scores(), "wallhaven", _Profile(n=10), [], 0)

    assert result.personalize == pytest.approx(95 * 10 / 15 + 60 * 5 / 15)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"quality": 59}, "quality"),
        ({"formulaic": 71}, "formulaic"),
        ({"nsfw": True}, "nsfw"),
        ({"watermark": True}, "watermark"),
        ({"text_overlay": "major"}, "text_overlay"),
    ],
)
def test_hard_gate_flags_each_condition(overrides, reason):
    assert reason in hard_gate(_scores(**overrides))


def test_minor_overlay_costs_ten_quality():
    assert "quality" in hard_gate(_scores(quality=65, text_overlay="minor"))


def test_clean_scores_pass_the_gate():
    assert hard_gate(_scores()) == []


def test_primary_style_drives_rarity_and_diversity():
    sharp = _scores(style_tags=["anime", "landscape"])

    fresh = explore_score(sharp, "wallhaven", [])
    stale = explore_score(sharp, "wallhaven", [("bing", "anime")] * 30)

    assert stale < fresh


def test_rank_is_deterministic_for_the_same_date():
    decisions = [_decision(1, 50), _decision(2, 50), _decision(3, 50)]

    forward = [d.wallpaper_id for d in rank(decisions, "2026-10-18")]
    backward = [d.wallpaper_id for d in rank(list(reversed(decisions)), "2026-10-18")]

    assert forward == backward


def test_rank_orders_by_final_then_quality():
    decisions = [_decision(1, 40), _decision(2, 90), _decision(3, 40, quality=95)]

    ids = [d.wallpaper_id for d in rank(decisions, "2026-10-18")]

    assert ids[0] == 2
    assert ids[1] == 3


def test_a_generic_anime_scene_is_docked():
    profile = _Profile(n=10)
    profile.avoid_tags = ("anime",)
    plain = final_score(
        _scores(style_tags=["landscape"], creativity=40, formulaic=60),
        "wallhaven", profile, [], 0,
    )
    anime = final_score(
        _scores(style_tags=["anime-style"], creativity=40, formulaic=60),
        "wallhaven", profile, [], 0,
    )

    assert anime.final == pytest.approx(plain.final - 12)


def test_a_creative_anime_scene_is_exempt():
    profile = _Profile(n=10)
    profile.avoid_tags = ("anime",)
    plain = final_score(
        _scores(style_tags=["landscape"], creativity=80, formulaic=20),
        "wallhaven", profile, [], 0,
    )
    anime = final_score(
        _scores(style_tags=["anime-style"], creativity=80, formulaic=20),
        "wallhaven", profile, [], 0,
    )

    assert anime.final == pytest.approx(plain.final)


def test_a_surreal_mysterious_scene_is_boosted():
    profile = _Profile(n=10)
    profile.boost_tags = ("surreal", "mysterious")
    plain = final_score(_scores(style_tags=["landscape"]), "wallhaven", profile, [], 0)
    surreal = final_score(
        _scores(style_tags=["surreal-mood", "mysterious"], creativity=70, formulaic=20),
        "wallhaven", profile, [], 0,
    )

    # Two hits x 6 points, still under the 18-point cap.
    assert surreal.final == pytest.approx(plain.final + 12)


def test_the_boost_is_capped():
    profile = _Profile(n=10)
    profile.boost_tags = ("surreal", "mysterious", "ethereal", "dreamlike")
    plain = final_score(_scores(style_tags=["landscape"]), "wallhaven", profile, [], 0)
    stacked = final_score(
        _scores(
            style_tags=["surreal", "mysterious", "ethereal", "dreamlike"],
            creativity=70, formulaic=20,
        ),
        "wallhaven", profile, [], 0,
    )

    assert stacked.final == pytest.approx(plain.final + 18)


def test_a_moody_picture_gets_a_bonus_on_a_rainy_night():
    dark = _scores(mood_tags=["moody", "rain"], palette=["#0b0d12"])
    bright = _scores(mood_tags=["bright", "sunny"], palette=["#f2f4f6"])

    # WMO 61 = rain, 23:00.
    assert context_adjust(dark, 61, 23) > 0
    assert context_adjust(dark, 61, 23) > context_adjust(bright, 61, 23)


def test_a_bright_picture_gets_a_bonus_at_clear_noon():
    bright = _scores(mood_tags=["bright", "blue"], palette=["#eef3f7"])
    dark = _scores(mood_tags=["dark", "night"], palette=["#0b0d12"])

    assert context_adjust(bright, 0, 12) > 0
    assert context_adjust(bright, 0, 12) > context_adjust(dark, 0, 12)


def test_context_never_punishes_and_never_exceeds_its_weight():
    worst = _scores(mood_tags=["night"], palette=["#ffffff"])

    assert context_adjust(worst, 0, 12) >= 0
    for code, hour in ((0, 12), (61, 23), (71, 8), (95, 18), (3, 14)):
        assert context_adjust(_scores(), code, hour) <= CONTEXT_WEIGHT


def test_context_is_zero_when_nothing_is_known():
    # No palette, no matching mood: the old ranking must be untouched.
    bare = _scores(palette=[], mood_tags=["plain"])

    assert context_adjust(bare, 0, None) == 0
    assert context_adjust(bare, 61, 12) == 0
