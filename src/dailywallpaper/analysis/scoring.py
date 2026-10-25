# src/dailywallpaper/analysis/scoring.py
"""Hard gates and the 55/45 explore-personalize ranking (spec 7.x)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dailywallpaper.models import Decision
from dailywallpaper.recommend.weather import weather_fit, weather_tags

from .vision import VisionScores

QUALITY_FLOOR = 60
MINOR_OVERLAY_PENALTY = 10
FORMULAIC_CEILING = 70
# Above this much "template" the picture is not banned, it is ranked down:
# a generic idea dressed well still loses to an original one.
FORMULAIC_BASE = 20
FORMULAIC_PENALTY = 0.5
# A nudge, not a veto. creativity and novelty already carry 70% of the explore
# term and correlate -0.80 / -0.90 with formulaic, so an unbounded penalty was
# charging the same axis twice: at formulaic=70 it took 25 points, more than
# desktop fit (-9..+9) and the weather bonus (+5) together could give back.
# Eight keeps the "dress it down" intent at the scale of every other nudge.
FORMULAIC_PENALTY_CAP = 8.0
# When a learned sugar-water model is available it takes over from the
# formulaic nudge: the model separates sugar water at AUC 0.771 on a held-out
# live batch, where this score manages 0.577 (docs/PLAN-media-selection.md).
# Its 0..100 output maps onto at most this many points, the scale every other
# adjustment uses.
SUGAR_PENALTY_MAX = 10.0


def sugar_penalty(score) -> float:
    """Dock a picture that matches the learned sugar-water pattern.

    ``None`` (no model, or no known tags) and anything at or below the
    neutral 50 cost nothing -- silence must not be read as approval.
    """
    if score is None or score <= 50:
        return 0.0
    return SUGAR_PENALTY_MAX * (score - 50.0) / 50.0
EXPLORE_WEIGHT = 0.25
PERSONALIZE_WEIGHT = 0.75
COLD_START_PRIOR = 60.0
PRIOR_STRENGTH = 5.0
RARITY_WINDOW = 30
# The curator asked not to see random game/anime scenes. A picture that
# genuinely earns its subject (creative, not a template) is exempt, so this
# is a penalty rather than a hard gate.
AVOID_PENALTY = 12
# A "local" note ("this one is too dark") only softens similar pictures.
SOFT_AVOID_PENALTY = 6
AVOID_CREATIVITY_EXEMPT = 60
AVOID_FORMULAIC_LIMIT = 40
# What the curator asked for more of (surreal / mysterious). Capped so a
# picture cannot win by stacking tag words on a thin idea.
BOOST_PER_TAG = 6
BOOST_CAP = 18
# Desktop-fit (local brightness/edge measurement) nudges, it does not decide:
# a beautiful picture that hides every icon should lose to a slightly worse
# one that works on a desk, but only by a few points.
DESKTOP_FIT_CENTER = 60.0
DESKTOP_FIT_WEIGHT = 0.15
# "Right now" fit (weather + hour) is a bonus of at most this many points.
# It reorders near-ties; it never overturns the 55/45 taste ranking.
CONTEXT_WEIGHT = 5.0
# How "dark" each weather group makes the moment feel, and how much the
# hour does; the picture's palette is compared against the mix.
_WEATHER_DARKNESS = (
    (0, 1, 0.0), (2, 3, 0.25), (45, 48, 0.4), (51, 67, 0.75),
    (71, 77, 0.3), (80, 82, 0.75), (85, 86, 0.3), (95, 99, 0.9),
)


def _weather_darkness(code) -> float:
    try:
        value = int(code)
    except (TypeError, ValueError):
        return 0.3
    for low, high, darkness in _WEATHER_DARKNESS:
        if low <= value <= high:
            return darkness
    return 0.3


def _hour_darkness(hour) -> float:
    if hour is None:
        return 0.0
    if hour >= 22 or hour <= 5:
        return 1.0
    if 18 <= hour <= 21:
        return 0.5
    if 6 <= hour <= 7:
        return 0.2
    return 0.0


def _palette_luminance(palette) -> float | None:
    """Mean perceived brightness of the model's palette, 0 (black) to 1."""
    values = []
    for text in palette or ():
        if not isinstance(text, str):
            continue
        digits = text.strip().lstrip("#")
        if len(digits) != 6:
            continue
        try:
            red = int(digits[0:2], 16)
            green = int(digits[2:4], 16)
            blue = int(digits[4:6], 16)
        except ValueError:
            continue
        values.append((0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255)
    if not values:
        return None
    return sum(values) / len(values)


def context_adjust(scores: VisionScores, weather_code: int,
                   hour: int | None = None) -> float:
    """0..CONTEXT_WEIGHT for a picture that suits the weather and the hour.

    Bonus-only on purpose: a rainy night should make a moody picture win a
    tie, not rule out the bright one the user keeps rating 9. Without a
    palette or a matching mood the adjustment is exactly zero, so the
    ranking of existing candidates is unchanged when nothing is known.
    """
    darkness = min(
        1.0,
        max(0.0, 0.65 * _weather_darkness(weather_code) + 0.35 * _hour_darkness(hour)),
    )
    expected = weather_tags(weather_code)
    if hour is not None:
        if hour >= 22 or hour <= 5:
            expected |= {"dark", "night", "moody"}
        elif 18 <= hour <= 21:
            expected |= {"warm", "cozy", "moody"}
        elif 6 <= hour <= 9:
            expected |= {"bright", "fresh", "warm"}
    bonus = 0.0
    luminance = _palette_luminance(getattr(scores, "palette", ()))
    if luminance is not None:
        target = 1.0 - darkness
        closeness = 1.0 - abs(luminance - target)
        # A clear fit earns credit; a mismatch simply earns none.
        if closeness > 0.6:
            bonus += 0.6 * min(1.0, (closeness - 0.6) / 0.4)
    moods = {
        tag.strip().lower()
        for tag in (getattr(scores, "mood_tags", ()) or ())
        if isinstance(tag, str)
    }
    if expected and moods & expected:
        bonus += 0.4
    return CONTEXT_WEIGHT * min(1.0, bonus)


def hard_gate(scores: VisionScores) -> list[str]:
    """Return every reason a candidate must not be applied."""
    quality = scores.quality - (
        MINOR_OVERLAY_PENALTY if scores.text_overlay == "minor" else 0
    )
    reasons: list[str] = []
    if quality < QUALITY_FLOOR:
        reasons.append("quality")
    if scores.formulaic > FORMULAIC_CEILING:
        reasons.append("formulaic")
    if scores.nsfw:
        reasons.append("nsfw")
    if scores.watermark:
        reasons.append("watermark")
    if scores.text_overlay == "major":
        reasons.append("text_overlay")
    return reasons


@dataclass(frozen=True)
class DecisionScores:
    explore: float
    personalize: float
    final: float


def explore_score(scores: VisionScores, source: str, recent) -> float:
    """Creativity/novelty plus how rare the primary style is and how much it differs from last time."""
    primary = scores.style_tags[0]
    count = sum(1 for _, style in recent if style == primary)
    rarity = 100 * (1 - min(count, RARITY_WINDOW) / RARITY_WINDOW)
    # recent is newest-first; the diversity term compares against the very
    # last applied wallpaper, not the oldest one in the window.
    last_source, last_style = recent[0] if recent else ("", "")
    diversity = 100
    if last_source == source:
        diversity -= 50
    if last_style == primary:
        diversity -= 50
    return (
        0.35 * scores.creativity
        + 0.35 * scores.novelty
        + 0.20 * rarity
        + 0.10 * max(0, diversity)
    )


def personalize_score(scores: VisionScores, profile, source: str, weather_code: int) -> float:
    # Mood counts as much as genre: the profile is built from the same
    # features, so a foggy quiet picture can match a taste learned from
    # another subject entirely.
    features = list(scores.style_tags) + list(scores.mood_tags)
    return (
        0.70 * profile.tag_affinity(features)
        + 0.20 * profile.source_affinity(source)
        + 0.10 * weather_fit(scores.mood_tags, weather_code)
    )


def avoid_penalty(scores: VisionScores, avoid_tags,
                  soft_avoid_tags=()) -> float:
    """Dock unwanted traits, without banning a whole category.

    ``avoid_tags`` are lasting dislikes (full weight); ``soft_avoid_tags``
    come from one picture's critique (half weight, so one dark picture does
    not blacklist every dark wallpaper). A genuinely creative picture is
    exempt either way.
    """
    hard = 0
    soft = 0
    for tag in scores.style_tags:
        lowered = tag.lower()
        if avoid_tags and any(key in lowered for key in avoid_tags):
            hard += 1
        elif soft_avoid_tags and any(key in lowered for key in soft_avoid_tags):
            soft += 1
    if not hard and not soft:
        return 0.0
    if (
        scores.creativity >= AVOID_CREATIVITY_EXEMPT
        and scores.formulaic <= AVOID_FORMULAIC_LIMIT
    ):
        return 0.0
    return AVOID_PENALTY * hard + SOFT_AVOID_PENALTY * soft


def boost_score(scores: VisionScores, boost_tags) -> float:
    """Credit the traits the curator asked for (surreal, mysterious, ...)."""
    if not boost_tags:
        return 0.0
    hits = sum(
        1
        for tag in scores.style_tags
        if any(key in tag.lower() for key in boost_tags)
    )
    return min(BOOST_CAP, BOOST_PER_TAG * hits)


def final_score(
    scores: VisionScores, source: str, profile, recent, weather_code: int,
    desktop_fit: float | None = None, hour: int | None = None,
    sugar: float | None = None,
) -> DecisionScores:
    explore = explore_score(scores, source, recent)
    raw_personalize = personalize_score(scores, profile, source, weather_code)
    n = profile.rating_count
    if n:
        personalize = (n / (n + PRIOR_STRENGTH)) * raw_personalize + (
            PRIOR_STRENGTH / (n + PRIOR_STRENGTH)
        ) * COLD_START_PRIOR
    else:
        personalize = COLD_START_PRIOR
    if sugar is None:
        penalty = min(
            FORMULAIC_PENALTY_CAP,
            FORMULAIC_PENALTY * max(0, scores.formulaic - FORMULAIC_BASE),
        )
    else:
        # A model that knows the concept replaces the blunt score, rather than
        # stacking on top of it.
        penalty = sugar_penalty(sugar)
    penalty += avoid_penalty(
        scores,
        getattr(profile, "avoid_tags", ()),
        getattr(profile, "soft_avoid_tags", ()),
    )
    boost = boost_score(scores, getattr(profile, "boost_tags", ()))
    adjust = 0.0
    if desktop_fit is not None:
        adjust = (desktop_fit - DESKTOP_FIT_CENTER) * DESKTOP_FIT_WEIGHT
    return DecisionScores(
        explore=explore,
        personalize=personalize,
        final=EXPLORE_WEIGHT * explore + PERSONALIZE_WEIGHT * personalize
        - penalty
        + boost
        + adjust
        + context_adjust(scores, weather_code, hour),
    )


def rank(decisions: list[Decision], date_key: str) -> list[Decision]:
    """Deterministic ordering: final, then quality, then a per-day stable hash."""

    def key(decision: Decision):
        tiebreak = hashlib.sha256(
            f"{decision.wallpaper_id}:{date_key}".encode()
        ).hexdigest()
        return (-decision.final, -decision.scores.quality, tiebreak)

    return sorted(decisions, key=key)
