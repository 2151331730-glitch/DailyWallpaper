# tests/test_first_run.py
"""First run: the optional Steam key step and the line it leaves in the report."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from dailywallpaper.ui import first_run


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.closeAllWindows()
    for widget in QApplication.allWidgets():
        widget.deleteLater()
    app.processEvents()


class _Env:
    """Just enough of steam_env.Detection for the floor the prompt checks."""

    def __init__(self, we_install_dir=None):
        self.we_install_dir = we_install_dir
        self.steam_root = None
        self.we_owned = False


def _stored(monkeypatch, value):
    monkeypatch.setattr(first_run.steam_key, "stored", lambda: value)


def _record_prompt(monkeypatch, answer):
    calls = []

    def fake_get_text(*args, **kwargs):
        calls.append(args[2] if len(args) > 2 else "")
        return answer

    monkeypatch.setattr(first_run.QInputDialog, "getText", fake_get_text)
    return calls


def _record_store(monkeypatch):
    saved = []
    monkeypatch.setattr(first_run.steam_key, "store", saved.append)
    return saved


def test_report_names_the_missing_key_and_where_to_get_one(qapp, monkeypatch):
    _stored(monkeypatch, "")

    lines = first_run._steam_key_lines()

    joined = "\n".join(lines)
    assert "还没填 Steam Web API key" in joined
    assert "steamcommunity.com/dev/apikey" in joined
    # The promise must stay narrow: today it only picks from what is subscribed.
    assert "已订阅" in joined


def test_report_says_saved_once_a_key_is_stored(qapp, monkeypatch):
    _stored(monkeypatch, "abc123")

    lines = first_run._steam_key_lines()

    assert lines == ["· Steam Web API key 已保存（启用线上创意工坊后才会调用）。"]


def test_ready_report_carries_the_key_state(qapp, monkeypatch, tmp_path):
    _stored(monkeypatch, "")

    lines = first_run._ready_report(tmp_path)

    assert any("Steam Web API key" in line for line in lines)
    assert lines[0].startswith("· 已找到 Wallpaper Engine")


def test_prompt_is_skipped_without_wallpaper_engine(qapp, monkeypatch):
    _stored(monkeypatch, "")
    monkeypatch.setattr(first_run, "detect", lambda: _Env(None))
    calls = _record_prompt(monkeypatch, ("typed", True))

    first_run._ask_steam_key()

    assert calls == []


def test_prompt_is_skipped_when_a_key_is_already_stored(qapp, monkeypatch):
    _stored(monkeypatch, "abc123")
    monkeypatch.setattr(first_run, "detect", lambda: _Env("C:/we"))
    calls = _record_prompt(monkeypatch, ("typed", True))

    first_run._ask_steam_key()

    assert calls == []


def test_prompt_saves_a_typed_key(qapp, monkeypatch):
    _stored(monkeypatch, "")
    monkeypatch.setattr(first_run, "detect", lambda: _Env("C:/we"))
    _record_prompt(monkeypatch, ("  abc123  ", True))
    saved = _record_store(monkeypatch)

    first_run._ask_steam_key()

    assert saved == ["abc123"]


@pytest.mark.parametrize("answer", [("abc123", False), ("   ", True)])
def test_prompt_stores_nothing_on_cancel_or_blank(qapp, monkeypatch, answer):
    _stored(monkeypatch, "")
    monkeypatch.setattr(first_run, "detect", lambda: _Env("C:/we"))
    _record_prompt(monkeypatch, answer)
    saved = _record_store(monkeypatch)

    first_run._ask_steam_key()

    assert saved == []


def test_first_run_asks_for_the_key_before_the_report(
    qapp, monkeypatch, tmp_path
):
    monkeypatch.setenv("DAILYWALLPAPER_HOME", str(tmp_path))
    monkeypatch.setattr("keyring.get_password", lambda *a: "vision-key")
    order = []
    monkeypatch.setattr(
        first_run, "_ask_steam_key", lambda: order.append("steam-key")
    )
    monkeypatch.setattr(
        first_run, "_environment_report", lambda cfg: order.append("report") or "报告"
    )
    shown = []
    monkeypatch.setattr(
        first_run.QMessageBox,
        "information",
        lambda *args: shown.append(args[2]),
    )

    class _Cfg:
        vision = None

    assert first_run.ensure_first_run(_Cfg()) is True

    assert order == ["steam-key", "report"]
    assert shown and "报告" in shown[0]
    assert first_run.install_prefs()["first_run_done"] is True


def test_second_launch_shows_nothing(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("DAILYWALLPAPER_HOME", str(tmp_path))
    (tmp_path / "install_prefs.json").write_text(
        '{"first_run_done": true}', encoding="utf-8"
    )
    monkeypatch.setattr("keyring.get_password", lambda *a: "vision-key")
    monkeypatch.setattr(
        first_run, "_ask_steam_key", lambda: pytest.fail("prompted again")
    )
    monkeypatch.setattr(
        first_run.QMessageBox, "information", lambda *a: pytest.fail("reported again")
    )

    class _Cfg:
        vision = None

    assert first_run.ensure_first_run(_Cfg()) is True
