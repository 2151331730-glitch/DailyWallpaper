"""Store the Steam Web API key in Windows Credential Manager.

Create a free key at https://steamcommunity.com/dev/apikey while signed in
with the same Steam account that owns Wallpaper Engine.

The app's settings page ("Steam Web API key") does the same thing and is the
only way from inside the packaged build, which ships no scripts folder. Either
route writes the slot of the build it runs in: this script from the source tree
means ``DailyWallpaper`` / ``steam_api_key``.
"""
import getpass

import keyring

key = getpass.getpass("Steam Web API key: ").strip()
if not key:
    raise SystemExit("empty key")
keyring.set_password("DailyWallpaper", "steam_api_key", key)
print("stored in Windows Credential Manager")
