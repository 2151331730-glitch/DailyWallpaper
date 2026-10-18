# tests/test_prefilter.py
import numpy as np
from PIL import Image

from dailywallpaper.analysis.images import dhash
from dailywallpaper.analysis.prefilter import prefilter
from dailywallpaper.config import SourcesSection
from dailywallpaper.sources.base import Candidate


def _candidate(width=3840, height=2160, media_type="image"):
    return Candidate("wallhaven", "x", "", "", "", "", media_type, width, height, 0, [], {})


def _checkerboard(width=512, height=320, cell=8):
    arr = (((np.indices((height, width)).sum(axis=0) // cell) % 2) * 255).astype(np.uint8)
    return Image.fromarray(np.stack([arr] * 3, axis=-1))


def test_sharp_image_passes_blur(tmp_path):
    path = tmp_path / "sharp.png"
    _checkerboard().save(path)

    result = prefilter(path, _candidate(), SourcesSection(), [])

    assert "blur" not in result.reasons


def test_uniform_image_fails_blur(tmp_path):
    path = tmp_path / "gray.png"
    Image.new("RGB", (512, 320), (128, 128, 128)).save(path)

    result = prefilter(path, _candidate(), SourcesSection(), [])

    assert "blur" in result.reasons


def test_black_bars_rejected(tmp_path):
    arr = np.full((320, 512, 3), 200, dtype=np.uint8)
    arr[:20, :] = 0
    arr[-20:, :] = 0
    path = tmp_path / "bars.png"
    Image.fromarray(arr).save(path)

    result = prefilter(path, _candidate(), SourcesSection(), [])

    assert "black_bars" in result.reasons


def test_duplicate_rejected_by_hash(tmp_path):
    path = tmp_path / "dup.png"
    _checkerboard().save(path)

    result = prefilter(path, _candidate(), SourcesSection(), [dhash(path)])

    assert "duplicate" in result.reasons


def test_low_resolution_rejected(tmp_path):
    path = tmp_path / "small.png"
    _checkerboard(400, 300).save(path)

    result = prefilter(path, _candidate(1280, 720), SourcesSection(), [])

    assert "resolution" in result.reasons


def test_wrong_aspect_rejected(tmp_path):
    path = tmp_path / "tall.png"
    _checkerboard(300, 500).save(path)

    result = prefilter(path, _candidate(3000, 5000), SourcesSection(), [])

    assert "aspect" in result.reasons


def test_scene_without_dimensions_is_not_rejected_for_aspect(tmp_path):
    path = tmp_path / "scene.png"
    _checkerboard().save(path)

    result = prefilter(path, _candidate(0, 0, media_type="scene"), SourcesSection(), [])

    assert "aspect" not in result.reasons
    assert "resolution" not in result.reasons
