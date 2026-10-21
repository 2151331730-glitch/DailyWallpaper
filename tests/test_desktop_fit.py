import numpy as np
import pytest
from PIL import Image

from dailywallpaper.analysis.desktop_fit import measure
from dailywallpaper.analysis.scoring import final_score
from dailywallpaper.analysis.vision import VisionScores


def _flat(path, value):
    Image.fromarray(
        np.full((180, 320, 3), value, dtype=np.uint8)
    ).save(path, "PNG")
    return path


def test_a_mid_tone_plain_image_scores_well(tmp_path):
    fit = measure(_flat(tmp_path / "mid.png", 110))

    assert fit.score >= 95
    assert fit.edge_density < 0.05


def test_a_bright_image_scores_low(tmp_path):
    fit = measure(_flat(tmp_path / "bright.png", 240))

    assert fit.mean_luma > 200
    assert fit.score < 60


def test_a_noisy_image_is_docked(tmp_path):
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, (180, 320, 3), dtype=np.uint8)
    path = tmp_path / "noise.png"
    Image.fromarray(noise).save(path, "PNG")

    fit = measure(path)

    assert fit.edge_density > 0.3
    assert fit.score < 80


class _Profile:
    rating_count = 10

    def tag_affinity(self, tags):
        return 60.0

    def source_affinity(self, source):
        return 60.0


def _scores():
    return VisionScores(
        quality=80, creativity=70, novelty=60, formulaic=20,
        nsfw=False, watermark=False, text_overlay="none",
        style_tags=["landscape"], mood_tags=["calm"],
        palette=["#112233"], subject="s", reason="r",
    )


def test_desktop_fit_adjusts_the_final_score():
    plain = final_score(_scores(), "wallhaven", _Profile(), [], 0)
    bad = final_score(_scores(), "wallhaven", _Profile(), [], 0, desktop_fit=0)
    good = final_score(_scores(), "wallhaven", _Profile(), [], 0, desktop_fit=100)

    assert bad.final == pytest.approx(plain.final - 9)
    assert good.final == pytest.approx(plain.final + 6)
