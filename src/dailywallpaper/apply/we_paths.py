# src/dailywallpaper/apply/we_paths.py
"""Give Wallpaper Engine a path it can actually open.

WE reads the ``-control openWallpaper -file`` argument as ANSI (GBK on a
Chinese Windows) while Mural hands it a Unicode string, so a Chinese file name
arrives as ``U+FFFD`` mojibake. WE writes that broken path into its own
config, cannot find the file, paints nothing -- and Mural's blackout picture
stays on the desktop. Handing WE an ASCII-only path is the fix; the database,
the UI and the change log keep the real path.

Two mechanisms, cheapest first:

1. the DOS 8.3 short name (``GetShortPathNameW``) when the volume still
   generates them -- same file, same folder, nothing is created;
2. a link with an ASCII name, only for a self-contained resource (a video or
   ``scene.pkg``). A symbolic link costs no disk space and follows a Steam
   update; a hard link is the fallback when link creation is refused.

Anything still non-ASCII after both (a ``project.json`` in a Chinese-named
folder, a web wallpaper spread over several files) is reported as unsupported.
That is the honest answer: WE cannot load it, and pretending otherwise is what
left the desktop black.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import shutil
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

# A project manifest and a web entry page resolve their real asset *relative to
# themselves*, so linking one of them alone points WE at an empty folder.
_NOT_SELF_CONTAINED = {".json", ".html", ".htm", ".url"}


def _short_path(path: Path) -> Path | None:
    """The DOS 8.3 name of ``path`` when it is useful and ASCII."""
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:  # not Windows
        return None
    func = kernel32.GetShortPathNameW
    func.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    func.restype = wintypes.DWORD
    try:
        needed = func(str(path), None, 0)
    except OSError:
        return None
    if not needed:
        return None
    buffer = ctypes.create_unicode_buffer(needed + 1)
    if not func(str(path), buffer, needed + 1):
        return None
    short = buffer.value
    # A volume with 8.3 names turned off answers with the long path, which is
    # still non-ASCII -- that is "no answer", not an answer.
    if not short or not short.isascii():
        return None
    return Path(short)


def _digest(path: Path) -> str:
    return hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:12]


def _ascii_name(source: Path) -> str:
    suffix = source.suffix if source.suffix.isascii() else ".bin"
    if source.name.isascii():
        return source.name
    return f"{_digest(source)}{suffix}"


def alias_path(alias_root: Path, source: Path) -> Path:
    """Where the ASCII alias for ``source`` lives (whether or not it exists)."""
    return Path(alias_root) / _digest(source) / _ascii_name(source)


def _same_file(link: Path, source: Path) -> bool:
    try:
        return os.path.samefile(link, source)
    except OSError:
        return False


def _link(source: Path, alias_root: Path) -> Path | None:
    folder = Path(alias_root) / _digest(source)
    linked = alias_path(alias_root, source)
    try:
        if not str(folder).isascii():
            return None
        folder.mkdir(parents=True, exist_ok=True)
        if _same_file(linked, source):
            return linked
        if linked.exists() or linked.is_symlink():
            linked.unlink()
        try:
            # A symlink follows a workshop re-download instead of pinning the
            # old inode, and works across volumes.
            os.symlink(source, linked)
        except OSError:
            os.link(source, linked)
        # The source is only written for cleanup: a deleted workshop item must
        # not keep its alias (and, for a hard link, its bytes) alive forever.
        (folder / "source.txt").write_text(str(source), encoding="utf-8")
        return linked
    except OSError as exc:
        logger.warning(f"cannot give {source} an ASCII name: {exc}")
        return None


def ascii_path(path: Path, alias_root: Path | None) -> Path | None:
    """An ASCII path WE can open for ``path``; None when there is none."""
    path = Path(path)
    if str(path).isascii():
        return path
    short = _short_path(path)
    if short is not None:
        return short
    if alias_root is None or path.suffix.lower() in _NOT_SELF_CONTAINED:
        return None
    return _link(path, Path(alias_root))


def default_alias_root(data_dir: Path) -> Path:
    """Where aliases live: the data directory, or ProgramData when it is not ASCII."""
    preferred = Path(data_dir) / "we_ascii"
    if str(preferred).isascii():
        return preferred
    base = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")
    return base / "Mural" / "we_ascii"


def prune(alias_root: Path, keep: str = "") -> None:
    """Drop aliases whose source no longer exists, never the one WE is using."""
    root = Path(alias_root)
    if not str(root).isascii() or not root.is_dir():
        return
    keep_abs = os.path.normcase(os.path.abspath(keep)) if keep else ""
    for folder in root.iterdir():
        if not folder.is_dir() or not folder.name.isascii():
            continue
        try:
            source = (folder / "source.txt").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not source:
            continue
        if keep_abs:
            # WE's config names the alias, not the source; either spelling
            # means "this one is in use".
            folder_abs = os.path.normcase(os.path.abspath(str(folder)))
            if keep_abs.startswith(folder_abs + os.sep) or keep_abs == os.path.normcase(
                os.path.abspath(source)
            ):
                continue
        if Path(source).exists():
            continue
        shutil.rmtree(folder, ignore_errors=True)
        logger.info(f"we_ascii: dropped a stale alias for {source}")
