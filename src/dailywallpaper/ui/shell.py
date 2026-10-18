# src/dailywallpaper/ui/shell.py
"""Wire the tray, the window and the polling timer together."""
from __future__ import annotations

from PySide6.QtCore import QTimer

from dailywallpaper.ui.main_window import MainWindow
from dailywallpaper.ui.tray import TrayIcon

POLL_MS = 2000


class UiShell:
    """Owns the Qt objects; all product logic lives in CommandService."""

    def __init__(self, command_service, machine, on_quit, parent=None):
        self.service = command_service
        self.machine = machine
        self.on_quit = on_quit
        self.window = MainWindow(command_service, command_service.conn)
        self.tray = TrayIcon(command_service, on_open=self.show_window, on_quit=self._quit)
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(POLL_MS)

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()

    def _poll(self) -> None:
        self.window.refresh()
        self.tray.refresh()

    def _quit(self) -> None:
        self.poll_timer.stop()
        self.tray.hide()
        self.on_quit()

    def start(self) -> None:
        self.tray.show()


def build_gui(cfg) -> int:
    """Composition root for the desktop shell."""
    import sys

    from PySide6.QtWidgets import QApplication

    from dailywallpaper.app import build_application
    from dailywallpaper.state.machine import SystemClock
    from dailywallpaper.ui.commands import CommandService

    application = build_application(cfg)
    machine = application.machine
    service = CommandService(
        machine.conn, cfg.app, SystemClock(), machine, machine.apply_service
    )
    app = QApplication.instance() or QApplication(sys.argv)
    shell = UiShell(service, machine, on_quit=app.quit)
    shell.start()
    return app.exec()
