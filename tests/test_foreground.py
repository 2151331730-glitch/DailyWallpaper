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


def test_apply_is_deferred_while_a_game_is_running(tmp_path, monkeypatch):
    class _Applier:
        applied = []

        def snapshot(self):
            raise AssertionError("apply must not touch the desktop")

    service = ApplyService(conn=None, applier=_Applier())
    monkeypatch.setattr(foreground, "user_is_busy", lambda: True)

    assert service.apply(1, tmp_path / "a.jpg", "auto") is False
    assert "全屏" in service.last_error


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
