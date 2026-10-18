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
