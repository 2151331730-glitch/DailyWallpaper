# tests/test_images.py
import numpy as np
import pytest
from PIL import Image

from dailywallpaper.analysis.images import canonicalize, dhash, download, hamming


def _checkerboard(width=512, height=320, cell=8):
    arr = (((np.indices((height, width)).sum(axis=0) // cell) % 2) * 255).astype(np.uint8)
    return Image.fromarray(np.stack([arr] * 3, axis=-1))


def test_canonicalize_returns_digest_and_resizes(tmp_path):
    src = tmp_path / "big.png"
    Image.new("RGB", (4000, 3000), "white").save(src)

    dest, digest = canonicalize(src, tmp_path / "out")

    assert dest.exists()
    assert dest.name == f"{digest}.jpg"
    with Image.open(dest) as im:
        assert max(im.size) <= 1024


def test_canonicalize_is_deterministic(tmp_path):
    src = tmp_path / "big.png"
    _checkerboard(2000, 1200).save(src)

    _, first = canonicalize(src, tmp_path / "a")
    _, second = canonicalize(src, tmp_path / "b")

    assert first == second


def test_dhash_distance_small_for_resized_copies(tmp_path):
    # Coarse blocks, so downscaling to the 9x8 hash grid keeps the structure.
    base = _checkerboard(cell=64)
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    base.save(a)
    base.resize((256, 160)).save(b)

    assert hamming(dhash(a), dhash(b)) < 8


def test_dhash_is_hex_string(tmp_path):
    path = tmp_path / "a.png"
    _checkerboard().save(path)

    value = dhash(path)

    assert len(value) == 16
    int(value, 16)


class _FakeStreamResponse:
    def __init__(self, chunks):
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, size):
        return iter(self.chunks)


class _FakeSession:
    def __init__(self, chunks):
        self.chunks = chunks

    def get(self, url, stream=True, timeout=30):
        return _FakeStreamResponse(self.chunks)


def test_download_writes_file(tmp_path):
    dest = download(_FakeSession([b"abc", b"def"]), "https://x/y.jpg", tmp_path / "y.jpg")
    assert dest.read_bytes() == b"abcdef"


def test_download_rejects_oversized_payload(tmp_path):
    with pytest.raises(ValueError):
        download(
            _FakeSession([b"x" * 10, b"y" * 10]),
            "https://x/y.jpg",
            tmp_path / "y.jpg",
            max_bytes=15,
        )


def test_download_leaves_no_partial_file_on_oversize(tmp_path):
    dest = tmp_path / "y.jpg"

    with pytest.raises(ValueError):
        download(
            _FakeSession([b"x" * 10, b"y" * 10]),
            "https://x/y.jpg",
            dest,
            max_bytes=15,
        )

    assert not dest.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_canonicalize_leaves_no_temp_file_behind(tmp_path):
    src = tmp_path / "big.png"
    Image.new("RGB", (800, 600), "white").save(src)
    out = tmp_path / "out"

    canonicalize(src, out)

    assert list(out.glob("tmp*")) == []


def test_download_copies_a_local_candidate(tmp_path):
    source = tmp_path / "workshop" / "preview.jpg"
    source.parent.mkdir()
    source.write_bytes(b"local-bytes")
    dest = tmp_path / "cache" / "1.jpg"

    download(None, str(source), dest)

    assert dest.read_bytes() == b"local-bytes"


def test_download_rejects_an_oversized_local_file(tmp_path):
    source = tmp_path / "big.jpg"
    source.write_bytes(b"x" * 100)

    with pytest.raises(ValueError):
        download(None, str(source), tmp_path / "out.jpg", max_bytes=10)
