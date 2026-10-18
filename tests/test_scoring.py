# tests/test_scoring.py
from pathlib import Path

import pytest

from dailywallpaper.analysis.scoring import explore_score, final_score, hard_gate, rank
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


def test_final_is_55_45_of_explore_and_personalize():
    result = final_score(_scores(), "wallhaven", _Profile(n=10), [], 0)

    assert result.final == pytest.approx(0.55 * result.explore + 0.45 * result.personalize)


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
