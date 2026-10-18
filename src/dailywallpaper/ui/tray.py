# src/dailywallpaper/ui/tray.py
"""System tray icon and menu (spec 11.1)."""
from __future__ import annotations

from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

STATE_COLOURS = {
    "idle": "#6b7280",
    "busy": "#2563eb",
    "changed": "#16a34a",
    "error": "#dc2626",
}
PAUSE_OPTIONS = (("1 天", 1), ("7 天", 7), ("直到手动恢复", None))
BUBBLE_MS = 5000


def _dot(colour: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setBrush(QColor(colour))
    painter.setPen(QColor(colour))
    painter.drawEllipse(4, 4, 24, 24)
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, service, on_open, on_quit, parent=None, on_rate=None):
        super().__init__(_dot(STATE_COLOURS["idle"]), parent)
        self.service = service
        self.on_open = on_open
        self.on_quit = on_quit

        self.menu = QMenu()
        self.today_action = QAction("今日壁纸", self.menu)
        self.today_action.triggered.connect(lambda: self.on_open())
        self.change_action = QAction("换一张", self.menu)
        self.change_action.triggered.connect(self._change_now)
        self.undo_action = QAction("撤销上一张", self.menu)
        self.undo_action.triggered.connect(self._undo)
        self.like_action = QAction("喜欢", self.menu)
        self.like_action.triggered.connect(
            lambda: on_rate(1) if on_rate else None
        )
        self.dislike_action = QAction("不喜欢", self.menu)
        self.dislike_action.triggered.connect(
            lambda: on_rate(-1) if on_rate else None
        )

        self.pause_action = QAction("暂停自动更换", self.menu)
        self.pause_menu = QMenu("暂停自动更换", self.menu)
        for label, days in PAUSE_OPTIONS:
            action = QAction(label, self.pause_menu)
            action.triggered.connect(lambda _checked=False, span=days: self._pause(span))
            self.pause_menu.addAction(action)
        self.pause_action.setMenu(self.pause_menu)

        self.settings_action = QAction("设置", self.menu)
        self.quit_action = QAction("退出", self.menu)
        self.quit_action.triggered.connect(lambda: self.on_quit())

        for action in (
            self.today_action,
            self.change_action,
            self.undo_action,
            self.like_action,
            self.dislike_action,
            self.pause_action,
            self.settings_action,
        ):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.setContextMenu(self.menu)
        self.refresh()

    def _change_now(self):
        self.service.change_now()
        self.refresh()

    def _undo(self):
        self.service.undo()
        self.refresh()

    def _pause(self, days):
        self.service.pause(days)
        self.refresh()

    def refresh(self) -> None:
        snapshot = self.service.snapshot()
        self.change_action.setEnabled(snapshot.can_change)
        self.pause_action.setText("恢复自动更换" if snapshot.paused else "暂停自动更换")
        self.pause_menu.setEnabled(not snapshot.paused)

    def set_state(self, kind: str) -> None:
        self.setIcon(_dot(STATE_COLOURS.get(kind, STATE_COLOURS["idle"])))

    def notify(self, title: str, message: str) -> None:
        self.showMessage(
            title, message, QSystemTrayIcon.MessageIcon.Information, BUBBLE_MS
        )
