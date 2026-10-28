# tests/test_foreground.py
"""The gate that keeps the app off the screen while a game owns it."""
from __future__ import annotations

from dailywallpaper.apply import foreground
from dailywallpaper.apply.service import ApplyService


def test_a_fullscreen_d3d_app_counts_as_busy(monkeypatch):
    monkeypatch.setattr(
        foreground, "notification_state",
        lambda: foreground.QUNS_RUNNING_D3D_FULL_SCREEN,
    )
    monkeypatch.setattr(foreground, "fullscreen_foreground", lambda: None)

    assert foreground.user_is_busy() is True


def test_a_borderless_game_is_caught_by_the_window_rectangle(monkeypatch):
    # The shell reports "notifications accepted" for a borderless game, which
    # is exactly why the rectangle probe exists.
    monkeypatch.setattr(
        foreground, "notification_state", lambda: foreground.QUNS_ACCEPTS_NOTIFICATIONS
    )
    monkeypatch.setattr(foreground, "fullscreen_foreground", lambda: "UnityWndClass")

    assert foreground.user_is_busy() is True


def test_an_ordinary_desktop_is_not_busy(monkeypatch):
    monkeypatch.setattr(
        foreground, "notification_state", lambda: foreground.QUNS_ACCEPTS_NOTIFICATIONS
    )
    monkeypatch.setattr(foreground, "fullscreen_foreground", lambda: None)

    assert foreground.user_is_busy() is False


def test_locked_screen_and_quiet_time_are_not_reasons_to_stop(monkeypatch):
    for state in (foreground.QUNS_NOT_PRESENT, foreground.QUNS_QUIET_TIME):
        monkeypatch.setattr(foreground, "notification_state", lambda s=state: s)
        monkeypatch.setattr(foreground, "fullscreen_foreground", lambda: None)
        assert foreground.user_is_busy() is False


def test_an_unreadable_probe_fails_open(monkeypatch):
    monkeypatch.setattr(foreground, "notification_state", lambda: None)
    monkeypatch.setattr(foreground, "fullscreen_foreground", lambda: None)

    assert foreground.user_is_busy() is False


def test_a_store_app_in_the_foreground_is_not_a_fullscreen_game(monkeypatch):
    # Settings, Photos and Calculator all report QUNS_APP while merely being
    # open; counting it froze the scheduler for as long as one stayed up.
    monkeypatch.setattr(foreground, "notification_state", lambda: foreground.QUNS_APP)
    monkeypatch.setattr(foreground, "fullscreen_foreground", lambda: None)

    assert foreground.user_is_busy() is False


def _rect(left, top, right, bottom):
    return foreground._RECT(left, top, right, bottom)


def test_a_window_that_fills_the_monitor_is_a_fullscreen_window():
    screen = _rect(0, 0, 2560, 1600)

    assert foreground._covers_monitor(_rect(0, 0, 2560, 1600), screen, False) is True
    # A game may round by a pixel.
    assert foreground._covers_monitor(_rect(1, 0, 2561, 1600), screen, False) is True


def test_a_maximised_window_is_never_a_game():
    # On a secondary monitor with no taskbar rcWork == rcMonitor, so a
    # maximised window's rectangle lands exactly on the monitor's.
    screen = _rect(2560, 0, 4480, 1440)

    assert foreground._covers_monitor(_rect(2560, 0, 4480, 1440), screen, True) is False
    # And a window that stops at the taskbar is not fullscreen either.
    assert foreground._covers_monitor(_rect(0, 0, 2560, 1560), _rect(0, 0, 2560, 1600), False) is False


def test_apply_is_deferred_while_a_game_is_running(tmp_path, monkeypatch):
    class _Applier:
        applied = []

        def snapshot(self):
            raise AssertionError("apply must not touch the desktop")

    service = ApplyService(conn=None, applier=_Applier())
    monkeypatch.setattr(foreground, "user_is_busy", lambda: True)

    assert service.apply(1, tmp_path / "a.jpg", "auto") is False
    assert "全屏" in service.last_error
    # The caller tells "declined for now" from "failed" by this flag alone.
    assert service.last_deferred is True


def test_a_later_failure_is_not_read_as_a_deferral(tmp_path, monkeypatch):
    from types import SimpleNamespace

    class _Applier:
        def snapshot(self):
            return None

        def apply(self, image_path, style=None):
            return SimpleNamespace(ok=False, error="boom")

    service = ApplyService(conn=None, applier=_Applier())
    monkeypatch.setattr(foreground, "user_is_busy", lambda: True)
    assert service.apply(1, tmp_path / "a.jpg", "auto") is False

    monkeypatch.setattr(foreground, "user_is_busy", lambda: False)
    assert service.apply(1, tmp_path / "a.jpg", "auto") is False
    # A real failure must not inherit the previous call's deferral.
    assert service.last_deferred is False


def test_the_guard_leaves_the_screen_alone_while_a_game_is_running(monkeypatch):
    class _We:
        calls = []

        def __getattr__(self, name):
            def record(*args, **kwargs):
                self.calls.append(name)
                raise AssertionError("the guard must not drive Wallpaper Engine")

            return record

    we = _We()
    service = ApplyService(conn=None, applier=object(), we=we)
    monkeypatch.setattr(foreground, "user_is_busy", lambda: True)

    assert service.keep_visible() is False
    assert we.calls == []
