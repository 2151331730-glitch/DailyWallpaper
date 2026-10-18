# src/dailywallpaper/ui/main_window.py
"""Today tab: current wallpaper, scores and the three actions (spec 11.2)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

PREVIEW_HEIGHT = 280

LATEST_SQL = """
select w.title, w.source, w.local_path, d.explore_score, d.personalize_score,
       d.final_score, d.reason
from decisions d join wallpapers w on w.id = d.wallpaper_id
order by d.id desc limit 1
"""


class MainWindow(QWidget):
    """Read-only view of the newest decision plus the two manual actions."""

    def __init__(self, service, conn, parent=None):
        super().__init__(parent)
        self.service = service
        self.conn = conn
        self.setWindowTitle("每日壁纸")
        self.resize(520, 620)

        self.preview_label = QLabel("还没有更换记录")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(PREVIEW_HEIGHT)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.meta_label = QLabel("")
        self.score_label = QLabel("")
        self.reason_label = QLabel("")
        self.reason_label.setWordWrap(True)

        self.change_button = QPushButton("换一张")
        self.change_button.clicked.connect(self._change_now)
        self.undo_button = QPushButton("撤销上一张")
        self.undo_button.clicked.connect(self._undo)

        actions = QHBoxLayout()
        actions.addWidget(self.change_button)
        actions.addWidget(self.undo_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preview_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.meta_label)
        layout.addWidget(self.score_label)
        layout.addWidget(self.reason_label)
        layout.addLayout(actions)
        self.refresh()

    def _change_now(self):
        self.service.change_now()
        self.refresh()

    def _undo(self):
        self.service.undo()
        self.refresh()

    def refresh(self) -> None:
        row = self.conn.execute(LATEST_SQL).fetchone()
        if not row:
            self.title_label.setText("")
            self.meta_label.setText("")
            self.score_label.setText("")
            self.reason_label.setText("")
        else:
            self.title_label.setText(row["title"] or "（无标题）")
            self.meta_label.setText(f"来源：{row['source']}")
            self.score_label.setText(
                f"E {row['explore_score']:.1f} · P {row['personalize_score']:.1f}"
                f" · 总分 {row['final_score']:.1f}"
            )
            self.reason_label.setText(row["reason"] or "")
            self._show_preview(row["local_path"])
        self.change_button.setEnabled(self.service.snapshot().can_change)

    def _show_preview(self, path: str) -> None:
        if not path:
            return
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
