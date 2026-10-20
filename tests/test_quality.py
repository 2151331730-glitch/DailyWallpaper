# tests/test_quality.py
import numpy as np
from PIL import Image

from dailywallpaper.analysis.quality import inspect_image, required_resolution


def _sharp(width, height):
    """A checkerboard: maximum detail, so the sharpness test cannot trip."""
    bands = (np.indices((height, width)).sum(axis=0) % 2) * 255
    return Image.fromarray(np.stack([bands] * 3, axis=-1).astype(np.uint8))


def test_a_thumbnail_is_rejected_for_resolution(tmp_path):
    path = tmp_path / "small.jpg"
    _sharp(432, 243).save(path, "JPEG")

    report = inspect_image(path, (1920, 1080))

    assert report.ok is False
    assert "resolution" in report.reasons
    assert (report.width, report.height) == (432, 243)


def test_a_full_size_sharp_image_passes(tmp_path):
    path = tmp_path / "big.jpg"
    _sharp(2560, 1440).save(path, "JPEG", quality=95)

    report = inspect_image(path, (1920, 1080))

    assert report.ok is True


def test_required_resolution_is_three_quarters_of_the_screen():
    assert required_resolution((2560, 1440)) == (1920, 1080)


def test_a_flat_image_is_rejected_as_blurry(tmp_path):
    path = tmp_path / "flat.jpg"
    Image.new("RGB", (2560, 1440), (120, 120, 120)).save(path, "JPEG")

    report = inspect_image(path, (1920, 1080))

    assert "blurry" in report.reasons


def test_an_unreadable_file_is_reported_not_raised(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image")

    report = inspect_image(path, (100, 100))

    assert report.ok is False
    assert any(reason.startswith("unreadable") for reason in report.reasons)
