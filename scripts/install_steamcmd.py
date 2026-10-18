"""Download Valve's SteamCMD into <data_dir>/tools/steamcmd.

The zip is about 3 MB; SteamCMD fetches the rest on first run. Run this once,
then scripts/steam_login.py once with the account that owns Wallpaper Engine.
"""
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dailywallpaper.config import load_config  # noqa: E402

URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
TIMEOUT = 120


def main() -> int:
    ROOT = Path(__file__).resolve().parents[1]
    cfg = load_config(ROOT / "config.toml")
    target = Path(cfg.app.data_dir) / "tools" / "steamcmd"
    target.mkdir(parents=True, exist_ok=True)
    exe = target / "steamcmd.exe"
    if exe.exists():
        print(f"already installed: {exe}")
        return 0
    print(f"downloading {URL} ...")
    with urllib.request.urlopen(URL, timeout=TIMEOUT) as response:
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(target)
    if not exe.exists():
        print("steamcmd.exe is missing after extraction", file=sys.stderr)
        return 1
    print(f"installed: {exe}")
    print("next: scripts/steam_login.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
