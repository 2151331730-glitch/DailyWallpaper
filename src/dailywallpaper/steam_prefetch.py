# src/dailywallpaper/steam_prefetch.py
"""Download the best Steam workshop items ahead of time.

The daily tick must never wait for a download, so remote candidates only enter
the shortlist once their ``project.json`` exists locally. This job runs on its
own daemon thread and quietly fills that cache.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

from dailywallpaper.steamcmd import SteamCmd, SteamCmdError

logger = logging.getLogger(__name__)

APP_ID = 431960
INTERVAL_SECONDS = 900
CANDIDATE_WINDOW = 50


class Prefetcher:
    def __init__(self, conn_factory, download_root: Path, steamcmd, account: str,
                 top_n: int = 2, max_cache_gb: float = 3.0, staging: Path | None = None,
                 sleep=time.sleep, interval: int = INTERVAL_SECONDS):
        self.conn_factory = conn_factory
        self.download_root = Path(download_root)
        self.steamcmd = steamcmd
        self.account = account
        self.top_n = top_n
        self.max_cache_gb = max_cache_gb
        self.staging = Path(staging) if staging else self.download_root / "_steamcmd"
        self.sleep = sleep
        self.interval = interval

    # -- one pass ------------------------------------------------------
    def run_once(self) -> list[str]:
        conn = self.conn_factory()
        try:
            rows = conn.execute(
                """
                select w.source_id, w.file_url, max(a.quality) as quality
                from wallpapers w
                join wallpaper_analyses wa on wa.wallpaper_id = w.id
                join analyses a on a.id = wa.analysis_id
                where w.source = 'steam_workshop'
                group by w.id
                order by quality desc
                limit ?
                """,
                (CANDIDATE_WINDOW,),
            ).fetchall()
            downloaded = []
            for row in rows:
                if len(downloaded) >= self.top_n:
                    break
                file_id = str(row["source_id"])
                if Path(row["file_url"]).is_file():
                    continue
                try:
                    project = self.steamcmd.download_item(
                        self.account, file_id, self.staging
                    )
                    self._install(Path(project).parent, file_id)
                    downloaded.append(file_id)
                    logger.info(f"steam workshop prefetch: {file_id} ready")
                except SteamCmdError as exc:
                    logger.warning(f"steam workshop prefetch {file_id} failed: {exc}")
            self._evict(conn)
            return downloaded
        finally:
            conn.close()

    def _install(self, item_dir: Path, file_id: str) -> None:
        dest = self.download_root / file_id
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        self.download_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item_dir), str(dest))

    def _evict(self, conn) -> None:
        """Keep the cache under max_cache_gb, newest items first."""
        limit = max(0.0, self.max_cache_gb) * 1024 ** 3
        entries = []
        for folder in self.download_root.iterdir():
            if not folder.is_dir() or folder == self.staging:
                continue
            size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
            entries.append((folder.stat().st_mtime, size, folder))
        total = sum(size for _, size, _ in entries)
        if total <= limit:
            return
        protected = self._applied_folder(conn)
        for _, size, folder in sorted(entries):
            if total <= limit:
                break
            if folder == protected:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            total -= size
            logger.info(f"steam workshop cache evicted: {folder.name}")

    def _applied_folder(self, conn) -> Path | None:
        row = conn.execute(
            """
            select w.file_url from change_log c
            join wallpapers w on w.id = c.wallpaper_id
            where c.success = 1
            order by c.id desc limit 1
            """
        ).fetchone()
        return Path(row[0]).parent if row and row[0] else None

    # -- daemon thread -------------------------------------------------
    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, name="steam-prefetch", daemon=True)
        thread.start()
        return thread

    def _loop(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception as exc:  # a bad pass must not kill the thread
                logger.warning(f"steam workshop prefetch failed: {exc}")
            self.sleep(self.interval)


def steamcmd_path(cfg) -> Path | None:
    """Where SteamCMD lives: config, then the project, then PATH."""
    configured = str(cfg.steam_workshop.steamcmd_path or "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    candidate = Path(cfg.app.data_dir) / "tools" / "steamcmd" / "steamcmd.exe"
    if candidate.is_file():
        return candidate
    found = shutil.which("steamcmd")
    return Path(found) if found else None


def build_prefetcher(cfg, conn_factory):
    """Return a ready Prefetcher, or None when it cannot run yet."""
    section = cfg.steam_workshop
    if not section.enabled:
        return None
    if not section.steam_account:
        logger.warning("steam workshop is on but steam_account is not set")
        return None
    exe = steamcmd_path(cfg)
    if exe is None:
        logger.warning(
            "steamcmd not found; run scripts/install_steamcmd.py or set "
            "[steam_workshop] steamcmd_path"
        )
        return None
    root = Path(cfg.app.data_dir) / "steam_workshop"
    return Prefetcher(
        conn_factory=conn_factory,
        download_root=root,
        steamcmd=SteamCmd(exe, timeout=section.download_timeout),
        account=section.steam_account,
        top_n=section.prefetch_top_n,
        max_cache_gb=section.max_cache_gb,
    )


def start_prefetcher(cfg):
    """Build and start the prefetch thread; errors are logged, never raised."""
    from dailywallpaper.db import connect

    prefetcher = build_prefetcher(
        cfg, lambda: connect(Path(cfg.app.data_dir) / "app.db")
    )
    if prefetcher is not None:
        prefetcher.start()
    return prefetcher
