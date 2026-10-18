# src/dailywallpaper/ui/shell.py
"""Wire the tray, the window and the polling timer together."""
from __future__ import annotations

from PySide6.QtCore import QTimer

from dailywallpaper.ui.main_window import MainWindow
from dailywallpaper.ui.tray import TrayIcon

POLL_MS = 2000


class UiShell:
    """Owns the Qt objects; all product logic lives in CommandService."""

    def __init__(self, command_service, machine, on_quit, parent=None, worker=None,
                 budget_cny=10.0):
        self.service = command_service
        self.machine = machine
        self.on_quit = on_quit
        self.worker = worker
        self.window = MainWindow(
            command_service,
            command_service.conn,
            budget_cny=budget_cny,
            config=getattr(command_service, "full_config", None),
            config_path=getattr(command_service, "config_path", None),
        )
        self.tray = TrayIcon(
            command_service,
            on_open=self.show_window,
            on_quit=self._quit,
            on_rate=self.rate,
        )
        self._notified_for = None
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(POLL_MS)

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()

    def rate(self, rating) -> None:
        from dailywallpaper.recommend.ratings import RatingsService

        row = self.service.conn.execute(
            "select wallpaper_id from decisions order by id desc limit 1"
        ).fetchone()
        if not row:
            return
        service = RatingsService(self.service.conn)
        if rating is not None:
            try:
                service.rate(row["wallpaper_id"], rating)
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning(f"rating failed: {exc}")
        else:
            from dailywallpaper.ui.rating_dialog import RatingDialog

            RatingDialog(service, row["wallpaper_id"], self.window).exec()
        self.window.refresh()

    def _poll(self) -> None:
        self.window.refresh()
        self.tray.refresh()
        today = self.service.clock.now().date().isoformat()
        if self.service.snapshot().today_changed:
            self.tray.set_state("changed")
            if self._notified_for != today:
                self._notified_for = today
                self.tray.notify("每日壁纸", "今日壁纸已更新")
        else:
            self.tray.set_state("idle")

    def _quit(self) -> None:
        self.poll_timer.stop()
        if self.worker is not None:
            self.worker.stop()
        self.tray.hide()
        self.on_quit()

    def start(self) -> None:
        self.tray.show()
        if self.worker is not None:
            self.worker.start()


def build_gui(cfg, config_path=None) -> int:
    """Composition root for the desktop shell."""
    import sys

    from PySide6.QtWidgets import QApplication

    from dailywallpaper.app import build_application
    from dailywallpaper.state.machine import SystemClock
    from dailywallpaper.ui.commands import CommandService

    from dailywallpaper.ui.worker import Worker

    application = build_application(cfg)
    machine = application.machine
    # The worker builds its own application inside its own thread: a sqlite
    # connection may only be used from the thread that created it.
    worker = Worker(machine_factory=lambda: build_application(cfg).machine)
    from dailywallpaper.config import default_config_path

    resolved_path = config_path or default_config_path()
    service = CommandService(
        machine.conn,
        cfg.app,
        SystemClock(),
        machine,
        machine.apply_service,
        runner=worker,
    )
    service.full_config = cfg
    service.config_path = resolved_path
    app = QApplication.instance() or QApplication(sys.argv)
    shell = UiShell(
        service, machine, on_quit=app.quit, worker=worker,
        budget_cny=cfg.budget.monthly_cny,
    )
    shell.start()
    return app.exec()
