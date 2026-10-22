# src/dailywallpaper/pipeline/materialize.py
"""Full-size downloads and the "never apply a screening copy" guard."""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from dailywallpaper.analysis.images import download
from dailywallpaper.analysis.quality import inspect_image, required_resolution
from dailywallpaper.apply.windows_static import convert_to_jpeg
from dailywallpaper.constants import MATERIALIZE_TOP_N

logger = logging.getLogger(__name__)


class MaterializeMixin:
    def _materialize(self, data_dir: Path, counters: dict) -> None:
        """Download the full image for the candidates that could be applied.

        Screening works on a <=1024px copy; applying that would upscale a
        thumbnail onto a 4K desktop. Anything without a real file is dropped
        rather than applied, so a failed download costs the day's wallpaper,
        not its quality.
        """
        kept = []
        made = 0
        for decision in self.shortlist:
            if decision.apply_path is not None:
                # Already a file on disk, or a local path with nothing better.
                kept.append(decision)
                continue
            # Beyond the head, leave the candidate in the shortlist without a
            # full-size file: pick() downloads it only if a click gets there.
            if made >= MATERIALIZE_TOP_N:
                kept.append(decision)
                continue
            row = self.conn.execute(
                "select file_url from wallpapers where id=?", (decision.wallpaper_id,)
            ).fetchone()
            url = str(row[0]) if row else ""
            if not url.startswith(("http://", "https://")):
                continue
            host = urlparse(url).netloc
            if self._host_failed(host):
                continue
            try:
                raw = data_dir / "downloads" / f"{decision.wallpaper_id}.img"
                download(
                    self.session,
                    url,
                    raw,
                    max_bytes=self.cfg.sources.max_image_mb * 1024 * 1024,
                )
                dest = data_dir / "wallpapers" / f"{decision.wallpaper_id}.jpg"
                convert_to_jpeg(raw, dest)
                raw.unlink(missing_ok=True)
                counters["downloads"] += 1
                report = inspect_image(dest, required_resolution())
                if not report.ok:
                    dest.unlink(missing_ok=True)
                    logger.warning(
                        f"full image for {decision.wallpaper_id} rejected by"
                        f" inspection: {report.width}x{report.height}"
                        f" sharpness {report.sharpness:.0f} {report.reasons}"
                    )
                    continue
                made += 1
                kept.append(replace(decision, apply_path=dest))
            except Exception as exc:
                if host:
                    self._failed_hosts[host] = time.time()
                logger.warning(
                    f"full image for {decision.wallpaper_id} failed: {exc}"
                )
        self.shortlist = kept

    def _materialize_one(self, decision):
        """Fetch the full image for the candidate a click is about to apply.

        Preparing ten full-size downloads per refill made every click wait on
        ten; the screening copy is not a wallpaper, so the file has to be
        fetched either way -- but only the one that actually goes on screen.
        """
        if decision.apply_path is not None:
            return decision
        row = self.conn.execute(
            "select file_url from wallpapers where id=?", (decision.wallpaper_id,)
        ).fetchone()
        url = str(row[0]) if row else ""
        if not url.startswith(("http://", "https://")):
            return None
        host = urlparse(url).netloc
        if self._host_failed(host):
            return None
        data_dir = Path(self.cfg.app.data_dir)
        try:
            raw = data_dir / "downloads" / f"{decision.wallpaper_id}.img"
            download(
                self.session,
                url,
                raw,
                max_bytes=self.cfg.sources.max_image_mb * 1024 * 1024,
            )
            dest = data_dir / "wallpapers" / f"{decision.wallpaper_id}.jpg"
            convert_to_jpeg(raw, dest)
            raw.unlink(missing_ok=True)
            report = inspect_image(dest, required_resolution())
            if not report.ok:
                dest.unlink(missing_ok=True)
                logger.warning(
                    f"full image for {decision.wallpaper_id} rejected by"
                    f" inspection: {report.width}x{report.height}"
                )
                return None
            return replace(decision, apply_path=dest)
        except Exception as exc:
            if host:
                self._failed_hosts[host] = time.time()
            logger.warning(
                f"full image for {decision.wallpaper_id} failed: {exc}"
            )
            return None

    def _keep_ready(self) -> None:
        self.shortlist = [decision for decision in self.shortlist if decision.ready]

    def _ffmpeg_available(self) -> bool:
        """A missing ffmpeg must not silently drop every video candidate."""
        if self._ffmpeg_ok is None:
            import shutil

            self._ffmpeg_ok = shutil.which(self.cfg.tools.ffmpeg_path) is not None
            if not self._ffmpeg_ok:
                logger.warning(
                    f"ffmpeg not found ({self.cfg.tools.ffmpeg_path}); "
                    "video candidates will be scored from their poster image"
                )
        return self._ffmpeg_ok
