# src/dailywallpaper/app_support.py
"""Small helpers shared by the pipeline and the composition root.

Kept out of app.py so that file can stay about orchestration instead of
accumulating every one-off utility next to the pipeline.
"""
from __future__ import annotations

import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


def interleave(batches) -> list:
    """Round-robin several lists so no single source owns the head."""
    if not batches:
        return []
    out = []
    longest = max(len(batch) for batch in batches)
    for index in range(longest):
        for batch in batches:
            if index < len(batch):
                out.append(batch[index])
    return out


def merge_tags(base, extra) -> tuple[str, ...]:
    """Ordered union, so a repeated tag is only matched once."""
    seen: list[str] = []
    for tag in tuple(base) + tuple(extra):
        if tag and tag not in seen:
            seen.append(tag)
    return tuple(seen)


def install_allows_seed() -> bool:
    """Did the installer checkbox say "use the author's default curation"?

    The choice is written once by the installer and lives next to the user's
    data, not in config.toml (the installer cannot safely edit TOML). Missing
    file means yes: source runs and older installs keep the seed.
    """
    from dailywallpaper.config import install_prefs

    return bool(install_prefs().get("use_seed", True))


def rate_limited(exc: Exception) -> bool:
    """True when the provider refused the call for being overloaded."""
    text = str(exc)
    return "429" in text or "1305" in text or "访问量过大" in text


def apply_path_for(candidate, canonical: Path) -> Path | None:
    """Where the applier should read the image from.

    A file already on disk (workshop project) is used directly; a remote
    original is marked None so the materializer downloads it full size. A
    local path that does not exist has nothing better than the screening copy.
    """
    source = str(candidate.file_url or "")
    if source and Path(source).is_file():
        return Path(source)
    if source.startswith(("http://", "https://")):
        return None
    if candidate.source in ("we_workshop", "steam_workshop"):
        # The asset vanished after the scan; the screening copy is not a
        # wallpaper and the project itself is gone.
        return None
    return canonical


def workshop_root(install_dir) -> Path | None:
    """<steamapps>/common/wallpaper_engine -> <steamapps>/workshop/content/431960."""
    path = Path(install_dir)
    parents = path.parents
    if len(parents) > 1 and parents[1].name.lower() == "steamapps":
        return parents[1] / "workshop" / "content" / "431960"
    return None


def configure_session(cfg, session=None) -> requests.Session:
    """A session whose proxy is the one in the config, and nothing else.

    ``trust_env`` must be off: with it on, requests falls back to the system
    or environment proxy, which is exactly the dead 127.0.0.1:7892 that made
    Bing fail even though the app had already decided to go direct.
    """
    session = session or requests.Session()
    session.trust_env = False
    proxy = str(getattr(cfg.sources, "proxy", "") or "").strip()
    if proxy:
        from dailywallpaper.net import proxy_alive

        if proxy_alive(proxy):
            # A live proxy is used for every source, domestic ones included.
            session.proxies.update({"http": proxy, "https": proxy})
        else:
            logger.warning(
                f"configured proxy {proxy} is not listening; using direct connections"
            )
    return session
