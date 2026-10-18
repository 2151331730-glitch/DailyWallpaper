import getpass, keyring
key = getpass.getpass("Zhipu API key: ").strip()
if not key:
    raise SystemExit("empty key")
keyring.set_password("DailyWallpaper", "zhipu", key)
print("stored in Windows Credential Manager")
