# tests/test_rating_entries.py
"""Every place the user can record a rating (spec 11.1-11.3)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from dailywallpaper.ui.commands import UiState
from dailywallpaper.ui.tray import TrayIcon


class _Service:
    def __init__(self):
        self.paused = False
        self.changed = 0

    def snapshot(self):
        return UiState(today_changed=False, paused=self.paused, can_change=True)

    def change_now(self):
        self.changed += 1
        return None

    def undo(self):
        return True


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.closeAllWindows()
    for widget in QApplication.allWidgets():
        widget.deleteLater()
    app.processEvents()


def _db(tmp_path):
    from dailywallpaper.db import connect, migrate

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn


def _wallpaper(conn, title="评分测试图"):
    conn.execute(
        "insert into wallpapers(source,source_id,title,created_at)"
        " values('bing','1',?,'x')",
        (title,),
    )
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-18',?,'70','80','74.5','r','applied','x')",
        (wid,),
    )
    conn.commit()
    return wid


def test_today_tab_has_a_rating_entry(qapp, tmp_path):
    from dailywallpaper.ui.main_window import MainWindow

    conn = _db(tmp_path)
    _wallpaper(conn)

    window = MainWindow(_Service(), conn)

    assert window.rate_button.text() == "评分…"


def test_today_tab_shows_the_existing_score(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.main_window import MainWindow

    conn = _db(tmp_path)
    wid = _wallpaper(conn)
    RatingsService(conn).rate(wid, 7)

    window = MainWindow(_Service(), conn)
    window.refresh()

    assert "7 / 10" in window.rating_status_label.text()


def test_today_tab_shows_the_live_wallpaper_not_the_latest_decision(qapp, tmp_path):
    from dailywallpaper.ui.main_window import MainWindow

    conn = _db(tmp_path)
    conn.execute(
        "insert into wallpapers(source,source_id,title,created_at)"
        " values('bing','live','桌面上的图','x')"
    )
    live = conn.execute("select max(id) from wallpapers").fetchone()[0]
    conn.execute(
        "insert into wallpapers(source,source_id,title,created_at)"
        " values('bing','newer','最新决策的图','x')"
    )
    newer = conn.execute("select max(id) from wallpapers").fetchone()[0]
    for wallpaper_id in (live, newer):
        conn.execute(
            "insert into decisions(decision_date,wallpaper_id,explore_score,"
            "personalize_score,final_score,reason,status,created_at)"
            " values('2026-10-18',?,'70','80','74.5','r','applied','x')",
            (wallpaper_id,),
        )
    conn.commit()

    class _LiveService(_Service):
        def current_wallpaper_id(self):
            return live

    window = MainWindow(_LiveService(), conn)
    window.refresh()

    assert "桌面上的图" in window.title_label.text()
    assert "最新决策的图" not in window.title_label.text()


def test_today_tab_disables_rating_without_a_decision(qapp, tmp_path):
    from dailywallpaper.ui.main_window import MainWindow

    conn = _db(tmp_path)

    window = MainWindow(_Service(), conn)

    assert window.rate_button.isEnabled() is False


def test_history_page_rates_the_selected_row(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.history_page import HistoryPage

    conn = _db(tmp_path)
    wid = _wallpaper(conn)
    seen = []
    page = HistoryPage(RatingsService(conn), on_undo=lambda: True, on_rate=seen.append)
    page.table.selectRow(0)

    page.rate_button.click()

    assert seen == [wid]


def test_history_page_double_click_rates_the_row(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.history_page import HistoryPage

    conn = _db(tmp_path)
    wid = _wallpaper(conn)
    seen = []
    page = HistoryPage(RatingsService(conn), on_undo=lambda: True, on_rate=seen.append)

    page.table.cellDoubleClicked.emit(0, 0)

    assert seen == [wid]


def test_history_page_rate_button_needs_a_selection(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.history_page import HistoryPage

    conn = _db(tmp_path)
    _wallpaper(conn)
    page = HistoryPage(RatingsService(conn), on_undo=lambda: True, on_rate=lambda _: None)
    page.table.selectRow(0)
    assert page.rate_button.isEnabled() is True

    page.refresh()

    assert page.rate_button.isEnabled() is False


def test_tray_rating_entry_opens_the_dialog(qapp):
    opened = []
    tray = TrayIcon(
        _Service(), on_open=lambda: None, on_quit=lambda: None,
        on_rate=lambda: opened.append(1),
    )

    tray.rate_action.trigger()

    assert opened == [1]


def test_rating_dialog_shows_the_wallpaper_it_rates(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = _db(tmp_path)
    wid = _wallpaper(conn, title="黄昏的湖面")

    dialog = RatingDialog(RatingsService(conn), wid)

    assert "黄昏的湖面" in dialog.title_label.text()


def test_rating_dialog_offers_the_ten_point_scale(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = _db(tmp_path)
    wid = _wallpaper(conn)

    dialog = RatingDialog(RatingsService(conn), wid)

    assert sorted(dialog.score_buttons) == list(range(1, 11))


def test_rating_dialog_saves_the_clicked_score(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = _db(tmp_path)
    wid = _wallpaper(conn)

    dialog = RatingDialog(RatingsService(conn), wid)
    dialog.score_buttons[7].click()

    assert dialog.chosen == 7
    assert conn.execute("select rating from ratings").fetchone()[0] == 7


def test_rating_dialog_prefills_the_existing_rating(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = _db(tmp_path)
    wid = _wallpaper(conn)
    RatingsService(conn).rate(wid, 8, comment="好看")

    dialog = RatingDialog(RatingsService(conn), wid)

    assert dialog.comment_edit.text() == "好看"
    assert "8 / 10" in dialog.current_label.text()
    assert dialog.score_buttons[8].isChecked() is True


def test_rating_dialog_restores_the_comment(qapp, tmp_path):
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = _db(tmp_path)
    wid = _wallpaper(conn)
    RatingsService(conn).rate(wid, 2, comment="太暗")

    dialog = RatingDialog(RatingsService(conn), wid)

    assert dialog.comment_edit.text() == "太暗"


def test_shell_rating_entry_opens_the_dialog_for_the_latest(qapp, tmp_path, monkeypatch):
    from datetime import datetime

    from dailywallpaper.config import AppSection
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.state.machine import TickResult
    from dailywallpaper.ui import rating_dialog as dialog_module
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

    opened = []

    class _Dialog:
        def __init__(self, service, wallpaper_id, parent=None):
            opened.append(wallpaper_id)

        def exec(self):
            return 0

    monkeypatch.setattr(dialog_module, "RatingDialog", _Dialog)
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    wid = _wallpaper(conn)
    service = CommandService(conn, AppSection(), _Clock(), _Machine(), _Apply())
    shell = UiShell(service, _Machine(), on_quit=lambda: None)

    shell.open_rating()

    assert opened == [wid]
    shell.poll_timer.stop()
