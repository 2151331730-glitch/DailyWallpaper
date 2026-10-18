# src/dailywallpaper/analysis/images.py
"""Image download, canonicalization and perceptual hashing."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image

DOWNLOAD_CHUNK = 1024 * 256
DOWNLOAD_TIMEOUT = 30
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
MAX_THUMB = 1024
JPEG_QUALITY = 90
DHASH_SIZE = (9, 8)


def download(session, url: str, dest: Path, max_bytes: int = MAX_DOWNLOAD_BYTES) -> Path:
    """Stream one URL to ``dest``, refusing payloads above ``max_bytes``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as response:
        response.raise_for_status()
        total = 0
        with dest.open("wb") as handle:
            for chunk in response.iter_content(DOWNLOAD_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("download too large")
                handle.write(chunk)
    return dest


def dhash(path: Path) -> str:
    """64-bit difference hash as a 16-character hex string."""
    with Image.open(path) as im:
        gray = im.convert("L").resize(DHASH_SIZE, Image.LANCZOS)
        pixels = np.asarray(gray, dtype=np.int16)
        bits = (pixels[:, 1:] > pixels[:, :-1]).flatten()
        return np.packbits(bits).tobytes().hex()


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def canonicalize(src: Path, dest_dir: Path) -> tuple[Path, str]:
    """Normalize to a <=1024px JPEG and name it by its content hash."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        converted = im.convert("RGB")
        converted.thumbnail((MAX_THUMB, MAX_THUMB), Image.LANCZOS)
        tmp = dest_dir / "tmp.jpg"
        converted.save(tmp, "JPEG", quality=JPEG_QUALITY)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    dest = dest_dir / f"{digest}.jpg"
    tmp.replace(dest)
    return dest, digest
