"""Download the essentials ffmpeg build into vendor/ffmpeg.

The build is ~100 MB and is not committed; run this once before packing.
Set HTTP_PROXY/HTTPS_PROXY when gyan.dev is slow from your network.
"""
from __future__ import annotations

import io
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
DEST = Path(__file__).resolve().parents[1] / "vendor" / "ffmpeg"


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        urllib.request.install_opener(
            urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        )
    print(f"downloading {URL}")
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = response.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if name.endswith("bin/ffmpeg.exe"):
                target = DEST / "ffmpeg.exe"
                with archive.open(name) as source:
                    target.write_bytes(source.read())
                print(f"wrote {target} ({target.stat().st_size / 1024 / 1024:.1f} MB)")
                return 0
    print("ffmpeg.exe not found in the archive", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
