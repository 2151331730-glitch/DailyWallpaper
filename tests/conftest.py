# tests/conftest.py
"""Suite-wide defaults that keep the tests independent of the desktop."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _nobody_is_playing_a_game(monkeypatch):
    """Never read the real foreground window during a test.

    ``ApplyService`` and ``Worker`` both ask whether a fullscreen app owns the
    screen. That probe looks at whatever is on the developer's monitor, so a
    fullscreen video left playing in another window would make unrelated
    tests defer their work and fail. Tests that need the busy state patch the
    probe themselves, which wins over this default.
    """
    from dailywallpaper.apply import foreground

    monkeypatch.setattr(foreground, "notification_state", lambda: None)
    monkeypatch.setattr(foreground, "fullscreen_foreground", lambda: None)
