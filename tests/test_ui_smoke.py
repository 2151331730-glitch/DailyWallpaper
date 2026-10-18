# tests/test_ui_smoke.py
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from dailywallpaper.state.machine import TickResult
from dailywallpaper.ui.commands import UiState
from dailywallpaper.ui.tray import TrayIcon


class _Service:
    def __init__(self):
        self.paused = False
        self.pauses = []
        self.changed = 0

    def snapshot(self):
        return UiState(today_changed=False, paused=self.paused, can_change=True)

    def pause(self, days):
        self.pauses.append(days)
        self.paused = True

    def resume(self):
        self.paused = False

    def change_now(self):
        self.changed += 1
        return TickResult("applied")

    def undo(self):
        return True


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_tray_has_the_documented_menu(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)

    labels = [action.text() for action in tray.menu.actions() if action.text()]

    assert labels[:5] == ["今日壁纸", "换一张", "撤销上一张", "暂停自动更换", "设置"]
    assert labels[-1] == "退出"


def test_pause_menu_offers_day_week_forever(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)

    labels = [action.text() for action in tray.pause_menu.actions()]

    assert labels == ["1 天", "7 天", "直到手动恢复"]


def test_pause_action_calls_the_service(qapp):
    service = _Service()
    tray = TrayIcon(service, on_open=lambda: None, on_quit=lambda: None)

    tray.pause_menu.actions()[1].trigger()

    assert service.pauses == [7]


def test_change_action_is_disabled_when_today_changed(qapp):
    class _Blocked(_Service):
        def snapshot(self):
            return UiState(today_changed=True, paused=False, can_change=False)

    tray = TrayIcon(_Blocked(), on_open=lambda: None, on_quit=lambda: None)

    assert tray.change_action.isEnabled() is False


def test_change_action_triggers_a_change(qapp):
    service = _Service()
    tray = TrayIcon(service, on_open=lambda: None, on_quit=lambda: None)

    tray.change_action.trigger()

    assert service.changed == 1


def test_state_switch_does_not_crash(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)

    for kind in ("idle", "busy", "changed", "error"):
        tray.set_state(kind)


def test_paused_service_hides_the_pause_menu(qapp):
    service = _Service()
    service.paused = True
    tray = TrayIcon(service, on_open=lambda: None, on_quit=lambda: None)

    assert tray.pause_action.text() == "恢复自动更换"
    assert tray.pause_menu.isEnabled() is False


def test_main_window_shows_the_latest_decision(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,title,created_at)"
        " values('bing','1','测试壁纸','x')"
    )
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,personalize_score,"
        "final_score,reason,status,created_at)"
        " values('2026-10-18',1,70,80,74.5,'reason','applied','x')"
    )
    conn.commit()

    window = MainWindow(_Service(), conn)
    window.refresh()

    assert "测试壁纸" in window.title_label.text()
    assert "74.5" in window.score_label.text()


def test_closing_the_window_hides_it(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    window = MainWindow(_Service(), conn)
    window.show()
    window.close()

    assert window.isVisible() is False


def test_window_without_history_does_not_crash(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    window = MainWindow(_Service(), conn)

    assert window.title_label.text() == ""


def test_shell_wires_tray_and_window(qapp, tmp_path):
    from datetime import datetime

    from dailywallpaper.config import AppSection
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.state.machine import TickResult
    from dailywallpaper.ui.commands import CommandService
    from dailywallpaper.ui.shell import UiShell

    class _Clock:
        def now(self):
            return datetime(2026, 10, 18, 9, 0)

    class _Machine:
        def tick(self, dry_run=False):
            return TickResult("kept")

    class _Apply:
        def undo_last(self):
            return True

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    service = CommandService(conn, AppSection(), _Clock(), _Machine(), _Apply())

    shell = UiShell(service, _Machine(), on_quit=lambda: None)

    assert shell.tray is not None
    assert shell.window is not None
    shell.poll_timer.stop()


def test_worker_ticks_once_on_start_with_its_own_machine():
    import threading

    from dailywallpaper.ui.worker import Worker

    seen = threading.Event()

    class _M:
        def tick(self, dry_run=False, manual=False):
            seen.set()

    worker = Worker(lambda: _M(), interval=60)
    worker.start()
    try:
        assert seen.wait(2) is True
    finally:
        worker.stop()


def test_worker_ignores_a_second_manual_request_while_busy():
    from dailywallpaper.ui.worker import Worker

    worker = Worker(lambda: object(), interval=60)

    assert worker.change_now() is True
    assert worker.change_now() is False
