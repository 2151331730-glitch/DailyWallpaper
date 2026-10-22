# src/dailywallpaper/ui/curation_window.py
"""A wall of thumbnails the user rates 1-10 to seed the recommender."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from dailywallpaper.recommend.ratings import RatingsService
from dailywallpaper.sources.repository import upsert_wallpaper

logger = logging.getLogger(__name__)

PAGE_SIZE = 40
COLUMNS = 5
THUMB_W = 200
THUMB_H = 125
COLORS = {
    **{value: "#dc2626" for value in range(1, 5)},
    **{value: "#9ca3af" for value in range(5, 7)},
    **{value: "#16a34a" for value in range(7, 11)},
}


class RateCard(QWidget):
    """One thumbnail with a 1-10 strip underneath."""

    rated = Signal(int, int)

    def __init__(self, wallpaper_id: int, thumb: Path | None, title: str,
                 rating: int | None = None):
        super().__init__()
        self.wallpaper_id = wallpaper_id
        self.rating = rating
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        self.image = QLabel()
        self.image.setFixedSize(THUMB_W, THUMB_H)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if thumb is not None and Path(thumb).is_file():
            pixmap = QPixmap(str(thumb))
            if not pixmap.isNull():
                self.image.setPixmap(
                    pixmap.scaled(
                        THUMB_W,
                        THUMB_H,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        layout.addWidget(self.image)

        caption = QLabel((title or "")[:28])
        caption.setStyleSheet("color:#9ca3af;font-size:10px;")
        layout.addWidget(caption)

        strip = QHBoxLayout()
        strip.setSpacing(1)
        self.buttons: dict[int, QToolButton] = {}
        for value in range(1, 11):
            button = QToolButton()
            button.setText(str(value))
            button.setFixedSize(18, 18)
            button.clicked.connect(
                lambda _checked=False, number=value: self._rate(number)
            )
            strip.addWidget(button)
            self.buttons[value] = button
        layout.addLayout(strip)
        self.set_rating(rating)

    def _rate(self, value: int) -> None:
        self.set_rating(value)
        self.rated.emit(self.wallpaper_id, value)

    def set_rating(self, rating: int | None) -> None:
        self.rating = rating
        for value, button in self.buttons.items():
            chosen = rating == value
            if chosen:
                button.setStyleSheet(
                    f"background:{COLORS[value]};color:#ffffff;font-size:9px;"
                    "border:1px solid #ffffff;border-radius:3px;"
                )
            else:
                button.setStyleSheet(
                    f"color:{COLORS[value]};font-size:9px;"
                    "border:1px solid #374151;border-radius:3px;"
                )
        border = "2px solid #16a34a" if rating is not None else "1px solid #1f2937"
        self.image.setStyleSheet(
            f"background:#111827;border:{border};border-radius:4px;"
        )


class _Worker(QObject):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    def run(self) -> None:
        try:
            self.done.emit(self.task())
        except Exception as exc:  # one failed seed build must not kill the app
            logger.warning(f"curation: seed build failed: {exc}")
            self.failed.emit(str(exc))


class CurationWindow(QDialog):
    """Rate every thumbnail, then build the seed when the user is ready."""

    def __init__(self, conn, candidates, thumbs, seed_builder, parent=None):
        super().__init__(parent)
        self.setWindowTitle("策展：给壁纸打分")
        self.resize(1180, 900)
        self.conn = conn
        self.thumbs = thumbs
        self.seed_builder = seed_builder
        self.ratings = RatingsService(conn)
        self.page = 0
        self.cards: dict[int, RateCard] = {}
        self.rows: list[tuple[object, int]] = []
        for candidate in candidates:
            try:
                wallpaper_id = upsert_wallpaper(conn, candidate)
            except Exception as exc:
                logger.warning(f"curation: cannot store {candidate.source_id}: {exc}")
                continue
            self.rows.append((candidate, wallpaper_id))

        self.progress = QLabel()
        self.progress.setStyleSheet("color:#e5e7eb;font-size:13px;")
        self.prev_button = QPushButton("上一页")
        self.prev_button.clicked.connect(lambda: self._flip(-1))
        self.next_button = QPushButton("下一页")
        self.next_button.clicked.connect(lambda: self._flip(1))
        self.page_label = QLabel()
        self.page_label.setStyleSheet("color:#9ca3af;")
        self.seed_button = QPushButton("生成种子")
        self.seed_button.clicked.connect(self._build_seed)
        self.status = QLabel("点缩略图下面的数字即可打分；1 分表示再也不想看到。")
        self.status.setStyleSheet("color:#9ca3af;font-size:11px;")

        header = QHBoxLayout()
        header.addWidget(self.progress)
        header.addStretch(1)
        header.addWidget(self.prev_button)
        header.addWidget(self.page_label)
        header.addWidget(self.next_button)
        header.addWidget(self.seed_button)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(6)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.status)
        self.setStyleSheet("QDialog{background:#0b1120;}")

        self._render()

    # -- pagination ----------------------------------------------------
    def _page_count(self) -> int:
        return max(1, (len(self.rows) + PAGE_SIZE - 1) // PAGE_SIZE)

    def _flip(self, step: int) -> None:
        self.page = max(0, min(self._page_count() - 1, self.page + step))
        self._render()

    def _render(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.cards.clear()
        start = self.page * PAGE_SIZE
        for offset, (candidate, wallpaper_id) in enumerate(
            self.rows[start : start + PAGE_SIZE]
        ):
            card = RateCard(
                wallpaper_id,
                self.thumbs.get(candidate.source_id),
                candidate.title or candidate.source_id,
                self.ratings.rating_for(wallpaper_id),
            )
            card.rated.connect(self._save_rating)
            self.grid.addWidget(card, offset // COLUMNS, offset % COLUMNS)
            self.cards[wallpaper_id] = card
        self.page_label.setText(f"{self.page + 1} / {self._page_count()}")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page < self._page_count() - 1)
        self._update_progress()

    def _save_rating(self, wallpaper_id: int, value: int) -> None:
        try:
            self.ratings.rate(wallpaper_id, value, comment="")
        except Exception as exc:
            logger.warning(f"curation: rating {wallpaper_id} not saved: {exc}")
        self._update_progress()

    def _rated_count(self) -> int:
        ids = [wallpaper_id for _, wallpaper_id in self.rows]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        row = self.conn.execute(
            f"select count(*) from ratings where wallpaper_id in ({marks})", ids
        ).fetchone()
        return int(row[0]) if row else 0

    def _update_progress(self) -> None:
        self.progress.setText(
            f"已评分 {self._rated_count()} / {len(self.rows)}"
        )

    # -- seed ----------------------------------------------------------
    def _build_seed(self) -> None:
        if self._rated_count() == 0:
            self.status.setText("还没有评分，先给几张图打分。")
            return
        self.seed_button.setEnabled(False)
        self.status.setText("正在分析已评分的图片并生成种子，请稍候……")
        worker = _Worker(self.seed_builder)
        worker.done.connect(self._seed_done)
        worker.failed.connect(self._seed_failed)
        self._worker = worker
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()

    def _seed_done(self, result) -> None:
        path, seed = result
        self.seed_button.setEnabled(True)
        self.status.setText(
            f"种子已生成：{path}（{seed.get('rating_count', 0)} 条评分）"
        )

    def _seed_failed(self, message: str) -> None:
        self.seed_button.setEnabled(True)
        self.status.setText(f"生成失败：{message}")


def run_curation(cfg, config_path=None, count: int = 300) -> int:
    """Open the curation wall: fetch, thumbnail, rate, export seed."""
    import sys

    from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

    from dailywallpaper.app import configure_session
    from dailywallpaper.config import load_config
    from dailywallpaper.analysis.vision import (
        KEYRING_ACCOUNT,
        KEYRING_SERVICE,
        VisionClient,
        ZhipuVision,
    )
    from dailywallpaper.cost.budget import BudgetGuard
    from dailywallpaper.curation import seed as seed_mod
    from dailywallpaper.curation.collect import collect_wallhaven, download_thumbs
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.state.machine import SystemClock

    import keyring

    app = QApplication.instance() or QApplication(sys.argv)
    data_dir = Path(cfg.app.data_dir)
    conn = connect(data_dir / "app.db")
    migrate(conn)
    session = configure_session(cfg)

    progress = QProgressDialog("正在抓取候选…", "取消", 0, count)
    progress.setWindowTitle("策展")
    progress.setWindowModality(Qt.WindowModality.WindowModal)
    progress.setMinimumDuration(0)
    progress.show()
    QApplication.processEvents()

    seen = [
        str(row[0])
        for row in conn.execute(
            "select source_id from wallpapers where source='wallhaven'"
        )
    ]
    candidates = collect_wallhaven(session, count, cfg.sources, skip_ids=seen)
    if progress.wasCanceled() or not candidates:
        progress.close()
        return 0

    progress.setLabelText("正在下载缩略图…")
    progress.setMaximum(len(candidates))
    thumbs_dir = data_dir / "curation" / "thumbs"

    def tick(index: int, total: int) -> None:
        progress.setValue(index)
        QApplication.processEvents()

    thumbs = download_thumbs(session, candidates, thumbs_dir, progress=tick)
    progress.close()
    if not thumbs:
        QMessageBox.warning(None, "策展", "一张缩略图都没下载成功，检查代理后重试。")
        return 1

    api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    if not api_key:
        QMessageBox.critical(None, "策展", "智谱 key 缺失，先运行 scripts/set_key.py。")
        return 1

    def build() -> tuple[Path, dict]:
        # sqlite connections are thread-bound, and this runs on a worker
        # thread: open a connection of its own instead of sharing the UI's.
        worker_conn = connect(data_dir / "app.db")
        try:
            made = seed_mod.analyze_rated(
                worker_conn,
                cfg.vision.screen_model,
                thumbs,
                cfg,
                api_key,
                data_dir,
                workers=4,
            )
            logger.info(f"curation: analysed {made} rated picture(s)")
            seed = seed_mod.build_seed(worker_conn)
            path = seed_mod.export_seed(seed)
            return path, seed
        finally:
            worker_conn.close()

    window = CurationWindow(conn, candidates, thumbs, build)
    window.show()
    return app.exec()
