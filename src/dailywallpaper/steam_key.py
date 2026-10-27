# src/dailywallpaper/steam_key.py
"""The optional Steam Web API key, and the one credential slot it lives in.

The key is not required: the online workshop source falls back to reading the
community page without it. What it buys is Steam's own API instead of HTML,
so the listing does not break when the page markup changes.

It sits in the same Windows credential store as the vision key, under its own
account name. That name predates ``identity.py`` and has to stay spelled the
same way -- renaming it orphans every key already pasted, and the uninstall
path deletes the slot by this exact name.
"""
from __future__ import annotations

ACCOUNT = "steam_api_key"


def stored() -> str:
    """The saved key, or "" when none is stored (or the store is locked)."""
    try:
        import keyring

        from dailywallpaper.analysis.vision import KEYRING_SERVICE

        return keyring.get_password(KEYRING_SERVICE, ACCOUNT) or ""
    except Exception:
        return ""


def store(key: str) -> None:
    """Write ``key`` to the slot. Raises when the store refuses the write."""
    import keyring

    from dailywallpaper.analysis.vision import KEYRING_SERVICE

    keyring.set_password(KEYRING_SERVICE, ACCOUNT, key)
