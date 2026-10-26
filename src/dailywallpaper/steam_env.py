# src/dailywallpaper/steam_env.py
"""Locate Steam and Wallpaper Engine the way the Steam client does.

A freshly installed copy ships with an empty ``[wallpaper_engine]``
``install_dir``, so without this module a new user has to hand-edit
``config.toml`` before any workshop wallpaper can appear. Steam publishes
its own root in the registry and every library folder in
``steamapps/libraryfolders.vdf``; reading the same files means the app finds
the game whichever drive it was installed on.
"""
from __future__ import annotations

import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path

APP_ID = "431960"
WE_FOLDER = "wallpaper_engine"
# WE ships both builds side by side and the 32-bit one is a perfectly normal
# engine (its own setting picks which one starts), so either file makes the
# folder usable. Requiring the 64-bit name hid a working install.
WE_EXES = ("wallpaper64.exe", "wallpaper32.exe")

logger = logging.getLogger(__name__)

_PAIR = re.compile(r'"([^"]+)"\s*"((?:[^"\\]|\\.)*)"')
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")

# The two pages a new user has to open to buy Wallpaper Engine (store) and
# subscribe to workshop items (community). Both are blocked from mainland
# China without help, which is exactly what the first-run report explains.
SITES = (
    ("Steam 商店", "https://store.steampowered.com/"),
    ("Steam 社区 / 创意工坊", "https://steamcommunity.com/"),
)

# A browser-like UA keeps the community site from answering an obvious bot
# with 403, which would otherwise look like a blocked network.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class SteamEnv:
    """What the local machine can tell us about Steam and Wallpaper Engine."""

    steam_root: Path | None = None
    library_folders: tuple[Path, ...] = ()
    we_install_dir: Path | None = None
    workshop_dir: Path | None = None
    we_owned: bool = False

    @property
    def usable(self) -> bool:
        """True when dynamic workshop wallpapers can be applied."""
        return self.we_install_dir is not None


def _unescape(value: str) -> str:
    """Undo the two escapes VDF applies to a quoted string."""
    return value.replace("\\\\", "\\").replace('\\"', '"')


def _registry_steam_root() -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows always ships winreg
        return None
    probes = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for root, key, name in probes:
        try:
            with winreg.OpenKey(root, key) as handle:
                raw = winreg.QueryValueEx(handle, name)[0]
        except OSError:
            continue
        path = Path(str(raw))
        if (path / "steamapps").is_dir():
            return path
    return None


def find_steam_root() -> Path | None:
    """The Steam install directory, or None when Steam is not installed."""
    found = _registry_steam_root()
    if found is not None:
        return found
    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        path = Path(guess)
        if (path / "steamapps").is_dir():
            return path
    return None


def library_folders(steam_root: Path) -> list[Path]:
    """The root library plus every extra one listed in libraryfolders.vdf.

    The file has two layouts in the wild: current clients store each library
    under a numeric key with a ``"path"`` line, older ones map the numeric
    key straight to the path. Both are read here.
    """
    candidates = [str(steam_root)]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for key, value in _PAIR.findall(text):
        if key == "path" or (key.isdigit() and _DRIVE_PATH.match(value)):
            candidates.append(_unescape(value))
    result: list[Path] = []
    for raw in candidates:
        path = Path(raw)
        if path not in result:
            result.append(path)
    return result


def is_wallpaper_engine_dir(install_dir: str | Path | None) -> bool:
    """True when ``install_dir`` really is a usable Wallpaper Engine folder.

    ``is_dir()`` alone is not enough: a mistyped or half-deleted folder would
    otherwise suppress detection forever and leave the app pointing at
    something that cannot be controlled.
    """
    if not install_dir:
        return False
    folder = Path(install_dir)
    return any((folder / name).is_file() for name in WE_EXES)


def workshop_dir_for(install: Path) -> Path | None:
    """``<steamapps>/common/wallpaper_engine`` -> ``workshop/content/431960``."""
    parents = install.parents
    if len(parents) > 1 and parents[1].name.lower() == "steamapps":
        return parents[1] / "workshop" / "content" / APP_ID
    return None


def detect() -> SteamEnv:
    """Scan the local Steam install for Wallpaper Engine and its workshop."""
    root = find_steam_root()
    if root is None:
        return SteamEnv()
    libraries = tuple(library_folders(root))
    install: Path | None = None
    owned = False
    for library in libraries:
        steamapps = library / "steamapps"
        if (steamapps / f"appmanifest_{APP_ID}.acf").is_file():
            owned = True
        candidate = steamapps / "common" / WE_FOLDER
        if install is None and is_wallpaper_engine_dir(candidate):
            install = candidate
    workshop = None
    if install is not None:
        workshop = workshop_dir_for(install)
    return SteamEnv(
        steam_root=root,
        library_folders=libraries,
        we_install_dir=install,
        workshop_dir=workshop,
        we_owned=owned,
    )


def apply_to_config(cfg):
    """Fill ``[wallpaper_engine]`` from the local Steam install.

    A path the user typed (or the settings page saved) always wins. When it
    is missing or broken, detection fills it in. The local workshop source is
    only ever turned **on**, never off, so a user who disabled it keeps it
    off -- ``auto_detect = false`` is the opt-out from this whole module.
    """
    we = cfg.wallpaper_engine
    if not getattr(we, "auto_detect", True):
        return cfg

    install: Path | None = None
    section = we
    if is_wallpaper_engine_dir(we.install_dir):
        install = Path(we.install_dir)
    else:
        env = detect()
        if env.we_install_dir is not None:
            install = env.we_install_dir
            section = replace(
                we,
                install_dir=str(install),
                config_path=str(install / "config.json"),
            )

    sources = cfg.sources
    if install is not None:
        workshop = workshop_dir_for(install)
        if (
            workshop is not None
            and workshop.is_dir()
            and not sources.we_workshop_enabled
        ):
            sources = replace(sources, we_workshop_enabled=True)
    if section is we and sources is cfg.sources:
        return cfg
    return replace(cfg, wallpaper_engine=section, sources=sources)


def _live_proxies(proxy: str):
    """The configured proxy, but only while something listens on it.

    Mirrors ``app_support.configure_session``: requests must not silently
    fall back to a dead system proxy, and a configured-but-dead proxy means
    direct connections, not "no network".
    """
    text = (proxy or "").strip()
    if not text:
        return None
    from dailywallpaper.net import proxy_alive

    if not proxy_alive(text):
        return None
    scheme = text if "://" in text else f"http://{text}"
    return {"http": scheme, "https": scheme}


def _probe(url: str, proxies, timeout: float) -> bool:
    import requests

    session = requests.Session()
    # Same rule as configure_session: never read the system/environment
    # proxy behind the user's back.
    session.trust_env = False
    if proxies:
        session.proxies.update(proxies)
    try:
        response = session.get(
            url,
            timeout=timeout,
            stream=True,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            return response.status_code < 400
        finally:
            response.close()
    except requests.RequestException:
        return False
    finally:
        session.close()


def check_sites(proxy: str = "", timeout: float = 3.0) -> dict[str, bool]:
    """Probe the store and community pages a new user must be able to open.

    Plain HTTP(S) is used rather than the Steam Web API because
    ``api.steampowered.com`` refuses the TLS handshake from mainland China
    even through a proxy, while the pages themselves still load.

    The two probes run concurrently, and the caller gets an answer after at
    most ``timeout + 1`` seconds even if DNS hangs: a first-run dialog must
    never look frozen.
    """
    proxies = _live_proxies(proxy)
    pool = ThreadPoolExecutor(max_workers=len(SITES))
    try:
        futures = [pool.submit(_probe, url, proxies, timeout) for _, url in SITES]
        done, _ = wait(futures, timeout=timeout + 1.0)
        values = []
        for future in futures:
            if future not in done or future.cancelled():
                values.append(False)
                continue
            try:
                values.append(future.result())
            except Exception:  # a probe must never escape as a crash
                values.append(False)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    # strict=True: one value per site is the whole contract above (every
    # future appends exactly one entry), so a mismatch is a bug to surface,
    # not something zip should swallow by truncating.
    return {name: ok for (name, _), ok in zip(SITES, values, strict=True)}
