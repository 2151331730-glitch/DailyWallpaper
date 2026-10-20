# src/dailywallpaper/ui/main_window.py
"""Main window: a today tab and a history tab (spec 11.2)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dailywallpaper.cost.report import CostService
from dailywallpaper.recommend.ratings import RatingsService, rating_text
from dailywallpaper.state.machine import SystemClock
from dailywallpaper.ui.cost_page import CostPage
from dailywallpaper.ui.settings_page import SettingsPage
from dailywallpaper.ui.history_page import HistoryPage

PREVIEW_HEIGHT = 280

LATEST_SQL = """
select d.id, w.title, w.source, w.local_path, d.explore_score, d.personalize_score,
       d.final_score, d.reason
from decisions d join wallpapers w on w.id = d.wallpaper_id
order by d.id desc limit 1
"""

DECISION_SQL = """
select d.id, w.title, w.source, w.local_path, d.explore_score, d.personalize_score,
       d.final_score, d.reason
from decisions d join wallpapers w on w.id = d.wallpaper_id
where d.wallpaper_id = ?
order by d.id desc limit 1
"""

WALLPAPER_SQL = """
select title, source, local_path from wallpapers where id=?
"""


class MainWindow(QWidget):
    """Read-only view of the newest decision plus the manual actions."""

    def __init__(self, service, conn, parent=None, budget_cny=10.0,
                 config=None, config_path=None, review_model=None):
        super().__init__(parent)
        self.service = service
        self.conn = conn
        self.review_model = review_model
        self.setWindowTitle("每日壁纸")
        icon_path = Path(__file__).resolve().parents[3] / "assets" / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(560, 660)

        self.tabs = QTabWidget()
        self.today = QWidget()
        self.rating_service = RatingsService(conn)
        self._build_today()
        self.history = HistoryPage(
            self.rating_service,
            on_undo=self._undo,
            on_rate=self._open_rating_for,
        )
        self.cost = CostPage(CostService(conn, SystemClock()), budget_cny)
        self.tabs.addTab(self.today, "今日")
        self.tabs.addTab(self.history, "历史")
        self.tabs.addTab(self.cost, "成本")
        self.settings = None
        if config is not None and config_path is not None:
            self.settings = SettingsPage(config, config_path)
            self.tabs.addTab(self.settings, "设置")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._polls = 0
        self._shown_decision_id = None

        outer = QVBoxLayout(self)
        outer.addWidget(self.tabs)
        self.refresh()

    def _build_today(self) -> None:
        self.preview_label = QLabel("还没有更换记录")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(PREVIEW_HEIGHT)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.meta_label = QLabel("")
        self.score_label = QLabel("")
        self.reason_label = QLabel("")
        self.reason_label.setWordWrap(True)
        self.reviewed_label = QLabel("")
        self.subscore_label = QLabel("")
        # Decoding a multi-MB wallpaper every 2 s poll is wasteful; the
        # normalised cache file is content-addressed, so the path is the key.
        self._preview_path = None

        self.change_button = QPushButton("换一张")
        self.change_button.clicked.connect(self._change_now)
        self.undo_button = QPushButton("撤销上一张")
        self.undo_button.clicked.connect(self._undo)
        self.rate_button = QPushButton("评分…")
        self.rate_button.clicked.connect(self._open_rating)
        self.rating_status_label = QLabel("")

        actions = QHBoxLayout()
        actions.addWidget(self.change_button)
        actions.addWidget(self.undo_button)
        actions.addWidget(self.rate_button)

        layout = QVBoxLayout(self.today)
        layout.addWidget(self.preview_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.meta_label)
        layout.addWidget(self.score_label)
        layout.addWidget(self.reason_label)
        layout.addWidget(self.subscore_label)
        layout.addWidget(self.reviewed_label)
        layout.addLayout(actions)
        layout.addWidget(self.rating_status_label)

    def _change_now(self):
        self.service.change_now()
        self.refresh()

    def _undo(self):
        self.service.undo()
        self.refresh()

    def _current_wallpaper_id(self):
        """Prefer the wallpaper actually on screen over the newest decision."""
        getter = getattr(self.service, "current_wallpaper_id", None)
        if getter is not None:
            try:
                found = getter()
            except Exception:
                found = None
            if found is not None:
                return int(found)
        row = self.conn.execute(
            "select wallpaper_id from decisions order by id desc limit 1"
        ).fetchone()
        return None if row is None else int(row["wallpaper_id"])

    def _open_rating(self):
        wallpaper_id = self._current_wallpaper_id()
        if wallpaper_id is None:
            return
        self._open_rating_for(wallpaper_id)

    def _open_rating_for(self, wallpaper_id: int) -> None:
        from dailywallpaper.ui.rating_dialog import RatingDialog

        RatingDialog(self.rating_service, wallpaper_id, self).exec()
        self.history.refresh()
        self.refresh()

    def _sync_rating_status(self, wallpaper_id=None) -> None:
        if wallpaper_id is None:
            wallpaper_id = self._current_wallpaper_id()
        rating = (
            self.rating_service.rating_for(wallpaper_id)
            if wallpaper_id is not None
            else None
        )
        self.rate_button.setEnabled(wallpaper_id is not None)
        if wallpaper_id is None:
            self.rating_status_label.setText("")
        elif rating is None:
            self.rating_status_label.setText("还没有评分")
        else:
            self.rating_status_label.setText(
                f"我的评分：{rating_text(rating)}（会影响明天的推荐）"
            )

    def refresh(self) -> None:
        wallpaper_id = self._current_wallpaper_id()
        row = None
        info = None
        if wallpaper_id is not None:
            row = self.conn.execute(DECISION_SQL, (wallpaper_id,)).fetchone()
            if row is None:
                info = self.conn.execute(WALLPAPER_SQL, (wallpaper_id,)).fetchone()
        else:
            row = self.conn.execute(LATEST_SQL).fetchone()
        self._shown_decision_id = None if row is None else int(row["id"])
        if row is not None:
            self.title_label.setText(row["title"] or "（无标题）")
            self.meta_label.setText(f"来源：{row['source']}")
            self.score_label.setText(
                f"E {row['explore_score']:.1f} · P {row['personalize_score']:.1f}"
                f" · 总分 {row['final_score']:.1f}"
            )
            self.reason_label.setText(row["reason"] or "")
            self._show_preview(row["local_path"])
        elif info is not None:
            # On the desktop but never scored by us (changed inside WE).
            self.title_label.setText(info["title"] or "（无标题）")
            self.meta_label.setText(f"来源：{info['source']} · 当前桌面壁纸")
            self.score_label.setText("")
            self.reason_label.setText("")
            self._show_preview(info["local_path"])
        else:
            self.title_label.setText("")
            self.meta_label.setText("")
            self.score_label.setText("")
            self.reason_label.setText("")
        self.subscore_label.setText(self._subscores())
        self.reviewed_label.setText(
            "已复核（付费模型）" if self._is_reviewed() else ""
        )
        self.change_button.setEnabled(self.service.snapshot().can_change)
        # Reuse the id resolved above: each lookup scans every wallpaper row
        # with Path.resolve(), and this runs on the 2 s poll.
        self._sync_rating_status(wallpaper_id)
        # Rebuilding 90 thumbnails on every 2 s poll would freeze the UI.
        self._polls += 1
        if self.tabs.currentWidget() is self.history and self._polls % 15 == 0:
            self.history.refresh()

    def _on_tab_changed(self, index):
        if self.tabs.currentWidget() is self.history:
            self.history.refresh()
        elif self.tabs.currentWidget() is self.cost:
            self.cost.refresh()

    def _subscores(self) -> str:
        if self._shown_decision_id is None:
            return ""
        row = self.conn.execute(
            """
            select a.quality, a.creativity, a.novelty, a.formulaic
            from decisions d
            join wallpaper_analyses wa on wa.wallpaper_id = d.wallpaper_id
            join analyses a on a.id = wa.analysis_id
            where d.id = ?
            order by a.id desc limit 1
            """,
            (self._shown_decision_id,),
        ).fetchone()
        if not row:
            return ""
        return (
            f"画质 {row['quality']:.0f} · 创意 {row['creativity']:.0f}"
            f" · 新颖 {row['novelty']:.0f} · 套路 {row['formulaic']:.0f}"
        )

    def _is_reviewed(self) -> bool:
        if not self.review_model or self._shown_decision_id is None:
            return False
        return (
            self.conn.execute(
                """
                select 1 from decisions d
                join wallpaper_analyses wa on wa.wallpaper_id = d.wallpaper_id
                join analyses a on a.id = wa.analysis_id
                where d.id = ? and a.model = ?
                limit 1
                """,
                (self._shown_decision_id, self.review_model),
            ).fetchone()
            is not None
        )

    def _show_preview(self, path: str) -> None:
        if not path or path == self._preview_path:
            return
        self._preview_path = path
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        self.preview_label.setPixmap(
            pixmap.scaled(
                self.preview_label.width(),
                PREVIEW_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def closeEvent(self, event):
        # Closing the window only hides it; quitting goes through the tray.
        self.hide()
        event.ignore()
