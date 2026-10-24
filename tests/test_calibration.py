# tests/test_calibration.py
from dailywallpaper.analysis.calibration import calibrate_scores
from dailywallpaper.analysis.vision import VisionScores


def _scores(**overrides):
    values = dict(
        quality=80,
        creativity=70,
        novelty=60,
        formulaic=20,
        nsfw=False,
        watermark=False,
        text_overlay="none",
        style_tags=["surreal"],
        mood_tags=["moody"],
        palette=["#112233"],
        subject="s",
        reason="r",
    )
    values.update(overrides)
    return VisionScores(**values)


def test_the_measured_offsets_are_added():
    calibrated = calibrate_scores(_scores())

    assert calibrated.creativity == 76
    assert calibrated.novelty == 63
    assert calibrated.formulaic == 18


def test_quality_is_left_alone():
    assert calibrate_scores(_scores(quality=55)).quality == 55


def test_tags_and_reason_come_through():
    calibrated = calibrate_scores(_scores())

    assert calibrated.style_tags == ["surreal"]
    assert calibrated.mood_tags == ["moody"]
    assert calibrated.reason == "r"


def test_scores_stay_inside_the_scale():
    calibrated = calibrate_scores(_scores(creativity=98, novelty=0, formulaic=0))

    assert calibrated.creativity == 100
    assert calibrated.novelty == 3
    assert calibrated.formulaic == 0
