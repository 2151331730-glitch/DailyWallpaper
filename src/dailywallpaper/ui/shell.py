# src/dailywallpaper/ui/shell.py
"""Wire the tray, the window and the polling timer together."""
from __future__ import annotations

from pathlib import Path

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
            review_model=getattr(command_service, "review_model", None),
        )
        config = getattr(command_service, "full_config", None)
        wallpapers_dir = (
            None if config is None else Path(config.app.data_dir) / "wallpapers"
        )
        self.tray = TrayIcon(
            command_service,
            on_open=self.show_window,
            on_quit=self._quit,
            on_rate=self.open_rating,
            wallpapers_dir=wallpapers_dir,
        )
        self._notified_for = None
        self._announced_seq = 0
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(POLL_MS)

    def show_window(self) -> None:
        # Poll refreshes are skipped while the window is hidden, so catch up
        # before the first paint instead of showing a stale frame.
        self.window.refresh()
        self.window.show()
        self.window.raise_()

    def open_rating(self) -> None:
        """Show the rating box for the wallpaper on screen right now."""
        from dailywallpaper.recommend.ratings import RatingsService
        from dailywallpaper.ui.rating_dialog import RatingDialog

        wallpaper_id = self.service.current_wallpaper_id()
        if wallpaper_id is None:
            return
        RatingDialog(
            RatingsService(self.service.conn), wallpaper_id, self.window
        ).exec()
        self.window.refresh()
        if self.window.tabs.currentWidget() is self.window.history:
            self.window.history.refresh()

    def _poll(self) -> None:
        # Resolving the desktop wallpaper costs a filesystem scan; doing it
        # while nobody is looking only makes the shell slower, so the hidden
        # window is refreshed on show instead.
        if self.window.isVisible():
            self.window.refresh()
        snapshot = self.service.snapshot()
        self.tray.refresh(snapshot)
        self._announce_manual_result()
        today = self.service.clock.now().date().isoformat()
        if self.worker is not None and self.worker.busy():
            self.tray.set_state("busy")
        elif getattr(self.worker, "last_action", "") in ("apply_failed", "tick_failed"):
            self.tray.set_state("error")
        elif snapshot.today_changed:
            self.tray.set_state("changed")
            if self._notified_for != today:
                self._notified_for = today
                self.tray.notify("每日壁纸", "今日壁纸已更新")
        else:
            self.tray.set_state("idle")

    def _announce_manual_result(self) -> None:
        """Say why a manual change ended without a new wallpaper.

        Silence looked like a dead click: the tray promised "正在挑选并更换"
        and nothing happened.
        """
        worker = self.worker
        if worker is None:
            return
        seq = getattr(worker, "result_seq", 0)
        if seq == self._announced_seq:
            return
        self._announced_seq = seq
        action, reason, manual = getattr(worker, "last_result", ("", "", False))
        if not manual:
            return
        if action == "kept":
            if reason == "no candidate passed":
                reason = "没有候选通过筛选（网络或模型限流）"
            self.tray.notify("没换成", reason or "没有可用的候选，稍后再试")
        elif action in ("apply_failed", "tick_failed"):
            self.tray.notify("更换失败", reason or "应用壁纸时出错，详情见日志")

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


def _start_prefetcher(cfg) -> None:
    """Fill the Steam workshop cache off the UI and tick threads."""
    from dailywallpaper.steam_prefetch import start_prefetcher

    try:
        start_prefetcher(cfg)
    except Exception as exc:  # prefetch is optional; never block the shell
        import logging

        logging.getLogger(__name__).warning(f"prefetcher not started: {exc}")


def _sync_autostart(cfg, config_path) -> None:
    """Heal a lost Run value at launch; the config is the source of truth."""
    from dailywallpaper.app_autostart import AutostartService, default_command

    try:
        AutostartService(command=default_command(config_path)).sync(
            cfg.app.start_with_windows
        )
    except Exception as exc:  # a locked registry must not block the shell
        import logging

        logging.getLogger(__name__).warning(f"autostart sync failed: {exc}")


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
    _start_prefetcher(cfg)
    from dailywallpaper.config import default_config_path

    resolved_path = config_path or default_config_path()
    _sync_autostart(cfg, resolved_path)
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
    # With one model doing both passes there is no separate "reviewed" stage
    # to advertise, and the window would label every wallpaper as reviewed.
    service.review_model = (
        cfg.vision.review_model
        if cfg.vision.review_model != cfg.vision.screen_model
        else None
    )
    app = QApplication.instance() or QApplication(sys.argv)
    shell = UiShell(
        service, machine, on_quit=app.quit, worker=worker,
        budget_cny=cfg.budget.monthly_cny,
    )
    shell.start()
    return app.exec()
