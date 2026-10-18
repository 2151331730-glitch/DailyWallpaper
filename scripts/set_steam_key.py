"""Store the Steam Web API key in Windows Credential Manager.

Create a free key at https://steamcommunity.com/dev/apikey while signed in
with the same Steam account that owns Wallpaper Engine.
"""
import getpass

import keyring

key = getpass.getpass("Steam Web API key: ").strip()
if not key:
    raise SystemExit("empty key")
keyring.set_password("DailyWallpaper", "steam_api_key", key)
print("stored in Windows Credential Manager")
