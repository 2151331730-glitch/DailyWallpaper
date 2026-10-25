# src/dailywallpaper/ui/tray.py
"""System tray icon and menu (spec 11.1)."""
from __future__ import annotations


from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from dailywallpaper.constants import DONATE_URL
from dailywallpaper.resources import asset

STATE_COLOURS = {
    "idle": "#6b7280",
    "busy": "#2563eb",
    "changed": "#16a34a",
    "error": "#dc2626",
}
STATE_LABELS = {
    "idle": "待机",
    "busy": "正在更换",
    "changed": "今日已更新",
    "error": "上次更换失败",
}
PAUSE_OPTIONS = (("1 天", 1), ("7 天", 7), ("直到手动恢复", None))
BUBBLE_MS = 5000
ICON_SIZE = 32
BADGE = 12
ICON_PATH = asset("icon.png")
# Decoding the icon on every 2 s poll would be wasteful; one pixmap serves all
# states. None means "asset missing", i.e. fall back to a plain coloured dot.
_base_icon = False


def _base() -> QPixmap | None:
    global _base_icon
    if _base_icon is False:
        pixmap = QPixmap(str(ICON_PATH))
        _base_icon = None if pixmap.isNull() else pixmap
    return _base_icon


def _icon(colour: str) -> QIcon:
    """The app icon with a state-coloured badge in the bottom-right corner.

    A naked coloured dot was unreadable in the tray: grey looked like the app
    had failed to start, and there was no way to tell a running idle app from
    a busy one. The picture stays recognisable; the badge carries the state.
    """
    pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    base = _base()
    if base is not None:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(
            0,
            0,
            base.scaled(
                ICON_SIZE,
                ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ),
        )
    else:
        painter.setBrush(QColor(colour))
        painter.setPen(QColor(colour))
        painter.drawEllipse(4, 4, 24, 24)
    offset = ICON_SIZE - BADGE
    painter.setBrush(QColor(colour))
    painter.setPen(QPen(QColor("#ffffff"), 2))
    painter.drawEllipse(offset, offset, BADGE, BADGE)
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, service, on_open, on_quit, parent=None, on_rate=None,
                 wallpapers_dir=None):
        super().__init__(_icon(STATE_COLOURS["idle"]), parent)
        self.wallpapers_dir = wallpapers_dir
        self.setToolTip(f"每日壁纸 · {STATE_LABELS['idle']}")
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
        # One entry that opens the rating box; the rating itself is one click
        # inside that box. Keeping three verbs in the tray menu made the menu
        # long and easy to hit by accident.
        self.rate_action = QAction("评分…", self.menu)
        self.rate_action.triggered.connect(
            lambda: on_rate() if on_rate else None
        )

        # Some live wallpapers (scene packages, videos) carry sound. One
        # checkable entry muting WE covers all of them; the per-wallpaper
        # volume lives inside WE, which this app does not run.
        self.sound_action = QAction("壁纸声音", self.menu)
        self.sound_action.setCheckable(True)
        probe = getattr(service, "sound_enabled", None)
        self.sound_action.setChecked(True if probe is None else bool(probe()))
        self.sound_action.triggered.connect(self._toggle_sound)

        self.pause_action = QAction("暂停自动更换", self.menu)
        self.pause_menu = QMenu("暂停自动更换", self.menu)
        for label, days in PAUSE_OPTIONS:
            action = QAction(label, self.pause_menu)
            action.triggered.connect(lambda _checked=False, span=days: self._pause(span))
            self.pause_menu.addAction(action)
        self.pause_action.setMenu(self.pause_menu)
        # While paused the submenu is removed, so a click resumes instead of
        # silently doing nothing (the "until manual" option never expires).
        self.pause_action.triggered.connect(self._pause_or_resume)

        self.settings_action = QAction("设置", self.menu)
        self.settings_action.triggered.connect(lambda: self.on_open())
        self.folder_action = QAction("打开壁纸文件夹", self.menu)
        self.folder_action.triggered.connect(self._open_folder)
        self.support_action = QAction("支持作者", self.menu)
        self.support_action.triggered.connect(self._open_support)
        self.quit_action = QAction("退出", self.menu)
        self.quit_action.triggered.connect(lambda: self.on_quit())

        for action in (
            self.today_action,
            self.change_action,
            self.undo_action,
            self.rate_action,
            self.sound_action,
            self.pause_action,
            self.settings_action,
            self.folder_action,
        ):
            self.menu.addAction(action)
        if DONATE_URL:
            self.menu.addAction(self.support_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.setContextMenu(self.menu)
        self.refresh()

    def _open_support(self) -> None:
        if DONATE_URL:
            QDesktopServices.openUrl(QUrl(DONATE_URL))

    def _open_folder(self) -> None:
        if self.wallpapers_dir is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.wallpapers_dir))
            )

    def _change_now(self):
        accepted = self.service.change_now()
        if accepted is False:
            self.notify("换一张", "上一次更换还没结束，这次点击已忽略")
        elif accepted is True:
            self.notify("换一张", "正在挑选并更换，请稍候")
        self.refresh()

    def _undo(self):
        if not self.service.undo():
            self.notify("撤销上一张", "没有可撤销的记录，或原壁纸文件已不存在")
        self.refresh()

    def _pause(self, days):
        self.service.pause(days)
        self.refresh()

    def _pause_or_resume(self):
        if self.service.snapshot().paused:
            self.service.resume()
        self.refresh()

    def _toggle_sound(self, checked: bool) -> None:
        setter = getattr(self.service, "set_sound", None)
        if setter is not None and not setter(checked):
            self.notify("壁纸声音", "切换失败，Wallpaper Engine 可能没有运行")
        # The command may have been ignored; show what the app actually holds.
        probe = getattr(self.service, "sound_enabled", None)
        if probe is not None:
            self.sound_action.setChecked(bool(probe()))

    def refresh(self, snapshot=None) -> None:
        # The shell polls every 2 s and already has a snapshot; asking the
        # service twice per poll was two extra queries for nothing.
        snapshot = snapshot or self.service.snapshot()
        self.change_action.setEnabled(snapshot.can_change)
        self.sound_action.setChecked(snapshot.sound_enabled)
        self.pause_action.setText("恢复自动更换" if snapshot.paused else "暂停自动更换")
        self.pause_action.setMenu(None if snapshot.paused else self.pause_menu)

    def set_state(self, kind: str) -> None:
        kind = kind if kind in STATE_COLOURS else "idle"
        self.setIcon(_icon(STATE_COLOURS[kind]))
        self.setToolTip(f"每日壁纸 · {STATE_LABELS[kind]}")

    def notify(self, title: str, message: str) -> None:
        self.showMessage(
            title, message, QSystemTrayIcon.MessageIcon.Information, BUBBLE_MS
        )
