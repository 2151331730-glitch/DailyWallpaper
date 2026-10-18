"""Sign SteamCMD in once so workshop downloads work without a password.

SteamCMD stores its own login token under tools/steamcmd; the app never sees
the password. Use the account that owns Wallpaper Engine.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dailywallpaper.config import default_config_path, load_config  # noqa: E402
from dailywallpaper.steam_prefetch import steamcmd_path  # noqa: E402


def main() -> int:
    cfg = load_config(default_config_path())
    exe = steamcmd_path(cfg)
    if exe is None:
        print("steamcmd not found; run scripts/install_steamcmd.py first", file=sys.stderr)
        return 1
    account = cfg.steam_workshop.steam_account.strip()
    if not account:
        account = input("Steam account name (not the password): ").strip()
    if not account:
        print("no account given", file=sys.stderr)
        return 1
    print(f"signing in as {account}; enter the password and Steam Guard here")
    # Interactive: this command must inherit the console so Steam Guard works.
    result = subprocess.run([str(exe), "+login", account, "+quit"])
    if result.returncode == 0:
        print("signed in; the token is cached for the app")
    else:
        print(f"sign-in failed (exit {result.returncode})", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
