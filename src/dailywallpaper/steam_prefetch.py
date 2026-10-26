# src/dailywallpaper/steam_prefetch.py
"""Download the best Steam workshop items ahead of time.

The daily tick must never wait for a download, so remote candidates only enter
the shortlist once their ``project.json`` exists locally. This job runs on its
own daemon thread and quietly fills that cache.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from dailywallpaper.steamcmd import SteamCmd

logger = logging.getLogger(__name__)

from dailywallpaper.constants import BANNED_MAX_RATING

APP_ID = 431960
INTERVAL_SECONDS = 900
CANDIDATE_WINDOW = 50
# Retry spacing for one broken item: 30 min, 1 h, 2 h, 4 h, 8 h, then weekly.
# A fixed 30-minute retry meant the same dead item re-ran -- and stalled the
# pass behind a 30-minute steamcmd timeout -- every half hour, forever.
FAILURE_RETRY_SECONDS = 30 * 60
FAILURE_MAX_COOLDOWN_SECONDS = 24 * 60 * 60
FAILURE_GIVE_UP_AFTER = 6
FAILURE_ABANDONED_RETRY_SECONDS = 7 * 24 * 60 * 60


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
                  and not exists (
                    select 1 from ratings r
                    where r.wallpaper_id = w.id and r.rating <= ?
                  )
                group by w.id
                order by quality desc
                limit ?
                """,
                (BANNED_MAX_RATING, CANDIDATE_WINDOW),
            ).fetchall()
            # The items we would prefetch next must survive eviction, or the
            # cache thrashes: best-ranked is downloaded first, then evicted
            # as the oldest, then downloaded again.
            # Protect exactly what we would prefetch; a wider window cannot
            # fit in max_cache_gb and would just be evicted again.
            protected = {str(row["source_id"]) for row in rows[: max(1, self.top_n)]}
            downloaded = []
            # Downloading into a full cache only makes the next eviction
            # delete the same item again.
            room = self._cache_bytes() < self._limit_bytes()
            for row in rows:
                if len(downloaded) >= self.top_n or not room:
                    break
                file_id = str(row["source_id"])
                if Path(row["file_url"]).is_file():
                    continue
                if self._recently_failed(conn, file_id):
                    continue
                try:
                    project = self.steamcmd.download_item(
                        self.account, file_id, self.staging
                    )
                    self._install(Path(project).parent, file_id)
                    self._clear_failure(conn, file_id)
                    downloaded.append(file_id)
                    logger.info(f"steam workshop prefetch: {file_id} ready")
                except Exception as exc:
                    # One bad item (timeout, missing exe, disk error) must not
                    # cost the rest of the pass.
                    self._mark_failure(conn, file_id)
                    logger.warning(f"steam workshop prefetch {file_id} failed: {exc}")
            self._evict(conn, protected)
            return downloaded
        finally:
            conn.close()

    def _install(self, item_dir: Path, file_id: str) -> None:
        dest = self.download_root / file_id
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        self.download_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item_dir), str(dest))

    def _limit_bytes(self) -> float:
        return max(0.0, self.max_cache_gb) * 1024 ** 3

    def _cache_bytes(self) -> float:
        if not self.download_root.is_dir():
            return 0.0
        return sum(
            sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
            for folder in self.download_root.iterdir()
            if folder.is_dir() and folder != self.staging
        )

    def _cooldown_seconds(self, count: int) -> float:
        """How long to wait after the ``count``-th consecutive failure."""
        if count >= FAILURE_GIVE_UP_AFTER:
            # Still not abandoned: the usual cause is the workshop being
            # unreachable for a while, so try again once a week instead of
            # never. A permanent skip would hide an item that was only unlucky.
            return FAILURE_ABANDONED_RETRY_SECONDS
        return min(
            FAILURE_RETRY_SECONDS * 2 ** max(0, count - 1),
            FAILURE_MAX_COOLDOWN_SECONDS,
        )

    def _failure(self, conn, file_id: str) -> tuple[float, int] | None:
        """(timestamp, consecutive failures) for an item, or None if it never failed."""
        row = conn.execute(
            "select value from state where key=?", (f"steam_failed_{file_id}",)
        ).fetchone()
        if row is None:
            return None
        raw = row[0]
        if isinstance(raw, str) and raw.lstrip().startswith("{"):
            try:
                data = json.loads(raw)
            except ValueError:
                return None
            if not isinstance(data, dict):
                return None
            try:
                ts = float(data.get("ts"))
            except (TypeError, ValueError):
                return None
            try:
                count = max(1, int(data.get("count") or 1))
            except (TypeError, ValueError):
                count = 1
            return ts, count
        # Rows written before the counter existed hold a bare timestamp.
        try:
            return float(raw), 1
        except (TypeError, ValueError):
            return None

    def _recently_failed(self, conn, file_id: str) -> bool:
        failure = self._failure(conn, file_id)
        if failure is None:
            return False
        ts, count = failure
        return time.time() - ts < self._cooldown_seconds(count)

    def _mark_failure(self, conn, file_id: str) -> None:
        failure = self._failure(conn, file_id)
        count = failure[1] + 1 if failure else 1
        conn.execute(
            "insert or replace into state(key,value) values(?,?)",
            (
                f"steam_failed_{file_id}",
                json.dumps({"ts": time.time(), "count": count}),
            ),
        )
        conn.commit()

    def _clear_failure(self, conn, file_id: str) -> None:
        conn.execute("delete from state where key=?", (f"steam_failed_{file_id}",))
        conn.commit()

    def _evict(self, conn, protected_ids=()) -> None:
        """Keep the cache under max_cache_gb, newest items first."""
        if not self.download_root.is_dir():
            return
        limit = self._limit_bytes()
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
            if folder == protected or folder.name in protected_ids:
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


def setup_ready(cfg) -> bool:
    """True when an online candidate could actually be downloaded today."""
    return (
        bool(str(cfg.steam_workshop.steam_account or "").strip())
        and steamcmd_path(cfg) is not None
    )


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
    proxy = str(getattr(cfg.sources, "proxy", "") or "").strip()
    if proxy:
        from dailywallpaper.net import proxy_alive

        if not proxy_alive(proxy):
            proxy = ""
    return Prefetcher(
        conn_factory=conn_factory,
        download_root=root,
        steamcmd=SteamCmd(
            exe,
            timeout=section.download_timeout,
            proxy=proxy,
        ),
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
