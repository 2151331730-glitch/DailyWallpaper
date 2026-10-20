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
    # Widgets outliving the QApplication crash the interpreter at exit under
    # the offscreen platform; tear them down while the app is still alive.
    app.closeAllWindows()
    for widget in QApplication.allWidgets():
        widget.deleteLater()
    app.processEvents()


def test_tray_has_the_documented_menu(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)

    labels = [action.text() for action in tray.menu.actions() if action.text()]

    assert labels[:6] == [
        "今日壁纸", "换一张", "撤销上一张", "评分…", "暂停自动更换", "设置",
    ]
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


def test_tray_tooltip_names_the_state(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)

    tray.set_state("busy")
    assert "正在更换" in tray.toolTip()

    tray.set_state("changed")
    assert "今日已更新" in tray.toolTip()

    tray.set_state("nonsense")  # unknown states fall back to idle
    assert "待机" in tray.toolTip()


def test_paused_service_offers_resume_instead_of_the_menu(qapp):
    service = _Service()
    service.paused = True
    tray = TrayIcon(service, on_open=lambda: None, on_quit=lambda: None)

    assert tray.pause_action.text() == "恢复自动更换"
    assert tray.pause_action.menu() is None
    tray.pause_action.trigger()
    assert service.paused is False


def test_folder_action_opens_the_wallpapers_dir(qapp, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile())),
    )
    tray = TrayIcon(
        _Service(), on_open=lambda: None, on_quit=lambda: None,
        wallpapers_dir=tmp_path,
    )

    tray.folder_action.trigger()

    assert opened == [tmp_path.as_posix()]
    assert any(action.text() == "打开壁纸文件夹" for action in tray.menu.actions())


def test_settings_action_opens_the_window(qapp):
    opened = []
    tray = TrayIcon(_Service(), on_open=lambda: opened.append(1), on_quit=lambda: None)

    tray.settings_action.trigger()

    assert opened == [1]


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


def test_refresh_resolves_the_current_wallpaper_once(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    window = MainWindow(_Service(), conn)
    calls = []
    window._current_wallpaper_id = lambda: (calls.append(1), 1)[1]

    window.refresh()

    # The poll runs every 2 s; resolving twice doubled a whole-table scan.
    assert calls == [1]


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


def test_worker_warms_the_shortlist_after_the_startup_tick():
    import threading
    from datetime import datetime

    from dailywallpaper.ui.worker import Worker

    warmed = threading.Event()

    class _Pipeline:
        def prepare(self, now, force=False):
            warmed.set()

    class _Clock:
        def now(self):
            return datetime(2026, 10, 19, 12, 0)

    class _M:
        pipeline = _Pipeline()
        clock = _Clock()

        def tick(self, dry_run=False, manual=False):
            pass

    worker = Worker(lambda: _M(), interval=60)
    worker.start()
    try:
        assert warmed.wait(2) is True
    finally:
        worker.stop()


def test_worker_reports_busy_while_a_tick_runs():
    import threading

    from dailywallpaper.ui.worker import Worker

    started = threading.Event()
    release = threading.Event()

    class _M:
        pipeline = None
        clock = None

        def tick(self, dry_run=False, manual=False):
            started.set()
            release.wait(5)

    worker = Worker(lambda: _M(), interval=60)
    worker.start()
    try:
        assert started.wait(2) is True
        assert worker.busy() is True
    finally:
        release.set()
        worker.stop()


def test_poll_shows_busy_while_the_worker_works(qapp, tmp_path):
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

    class _Worker:
        last_action = ""

        def busy(self):
            return True

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    service = CommandService(conn, AppSection(), _Clock(), _Machine(), _Apply())
    shell = UiShell(service, _Machine(), on_quit=lambda: None, worker=_Worker())
    try:
        shell._poll()
        assert "正在更换" in shell.tray.toolTip()
    finally:
        shell.poll_timer.stop()


def test_worker_ignores_a_second_manual_request_while_busy():
    from dailywallpaper.ui.worker import Worker

    worker = Worker(lambda: object(), interval=60)

    assert worker.change_now() is True
    assert worker.change_now() is False


def test_rating_dialog_saves_a_score_and_reason(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.commit()

    dialog = RatingDialog(RatingsService(conn), wid)
    dialog.reason_buttons["太暗"].click()
    dialog.score_buttons[8].click()

    assert dialog.chosen == 8
    row = conn.execute("select rating, comment from ratings").fetchone()
    assert row["rating"] == 8
    assert "太暗" in row["comment"]


def test_rating_dialog_stays_open_after_saving(qapp, tmp_path):
    from PySide6.QtWidgets import QDialog

    from dailywallpaper.db import connect, migrate
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.commit()

    dialog = RatingDialog(RatingsService(conn), wid)
    dialog.score_buttons[8].click()

    assert dialog.chosen == 8
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "已保存" in dialog.hint_label.text()


def test_reasons_typed_after_the_score_update_the_saved_row(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.commit()

    dialog = RatingDialog(RatingsService(conn), wid)
    dialog.score_buttons[8].click()
    dialog.reason_buttons["太暗"].click()

    row = conn.execute("select rating, comment from ratings").fetchone()
    assert row["rating"] == 8
    assert "太暗" in row["comment"]


def test_rating_dialog_opens_at_the_bottom_right(qapp, tmp_path):
    from PySide6.QtGui import QGuiApplication

    from dailywallpaper.db import connect, migrate
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog, CORNER_MARGIN

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.commit()

    dialog = RatingDialog(RatingsService(conn), wid)
    dialog.show()
    qapp.processEvents()
    area = QGuiApplication.primaryScreen().availableGeometry()
    try:
        assert dialog.x() == area.right() - dialog.width() - CORNER_MARGIN + 1
        assert dialog.y() == area.bottom() - dialog.height() - CORNER_MARGIN + 1
    finally:
        dialog.close()


def test_rating_dialog_exposes_the_documented_reasons(qapp):
    from dailywallpaper.ui.rating_dialog import QUICK_REASONS

    assert QUICK_REASONS == [
        "太花哨", "太暗", "不喜欢动漫", "想要风景",
        "想要暗色", "想要简约", "想要人物", "其他",
    ]


def test_history_page_lists_recent_decisions(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.history_page import HistoryPage

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,title,created_at) values('bing','1','历史图','x')"
    )
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-17',?,'70','80','74.5','r','applied','x')",
        (wid,),
    )
    conn.commit()

    page = HistoryPage(RatingsService(conn), on_undo=lambda: True)
    page.refresh()

    assert page.row_count == 1


def test_main_window_has_today_and_history_tabs(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    window = MainWindow(_Service(), conn)

    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "今日", "历史", "成本"
    ]


def test_tray_keeps_quick_ratings_out_of_the_menu(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)

    labels = [action.text() for action in tray.menu.actions() if action.text()]

    assert "评分…" in labels
    assert "喜欢" not in labels
    assert "中性" not in labels
    assert "不喜欢" not in labels


def test_tray_rate_action_calls_back(qapp):
    seen = []
    tray = TrayIcon(
        _Service(), on_open=lambda: None, on_quit=lambda: None,
        on_rate=lambda: seen.append(1),
    )

    tray.rate_action.trigger()

    assert seen == [1]


def test_cost_page_shows_spend_and_models(qapp, tmp_path):
    from datetime import datetime

    from dailywallpaper.cost.report import CostService
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.cost_page import CostPage

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into api_usage(ts,provider,model,purpose,cost_cny,status_code)"
        " values('2026-10-02T09:00:00','zhipu','glm-x','screen',0.5,200)"
    )
    conn.commit()

    class _Clock:
        def now(self):
            return datetime(2026, 10, 18, 12, 0)

    page = CostPage(CostService(conn, _Clock()), 10.0)
    page.refresh()

    assert "0.50" in page.spent_label.text()
    assert page.table.rowCount() == 1


def test_cost_page_handles_an_empty_ledger(qapp, tmp_path):
    from datetime import datetime

    from dailywallpaper.cost.report import CostService
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.cost_page import CostPage

    conn = connect(tmp_path / "app.db")
    migrate(conn)

    class _Clock:
        def now(self):
            return datetime(2026, 10, 18, 12, 0)

    page = CostPage(CostService(conn, _Clock()), 10.0)
    page.refresh()

    assert "0.00" in page.spent_label.text()
    assert page.table.rowCount() == 0


def _config_file(tmp_path):
    from dailywallpaper.config import (
        AppSection, BudgetSection, Config, RetentionSection, SourcesSection,
        ToolsSection, VisionSection, WallpaperEngineSection, WallpaperSection,
        WeatherSection, save_config,
    )

    cfg = Config(
        AppSection(), SourcesSection(), VisionSection(), BudgetSection(),
        WeatherSection(), WallpaperSection(), WallpaperEngineSection(),
        ToolsSection(), RetentionSection(),
    )
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    return cfg, path


class _RecordingAutostart:
    """Keeps the settings tests off the real HKCU Run key."""

    def __init__(self):
        self.calls = []

    def sync(self, wanted):
        self.calls.append(wanted)


def test_settings_page_saves_a_valid_change(qapp, tmp_path):
    from dailywallpaper.config import load_config
    from dailywallpaper.ui.settings_page import SettingsPage

    cfg, path = _config_file(tmp_path)
    page = SettingsPage(cfg, path, autostart=_RecordingAutostart())
    page.daily_time_edit.setText("07:15")

    assert page.save() is True
    assert load_config(path).app.daily_time == "07:15"


def test_settings_page_rejects_an_invalid_time(qapp, tmp_path):
    from dailywallpaper.config import load_config
    from dailywallpaper.ui.settings_page import SettingsPage

    cfg, path = _config_file(tmp_path)
    page = SettingsPage(cfg, path, autostart=_RecordingAutostart())
    page.daily_time_edit.setText("25:99")

    assert page.save() is False
    assert load_config(path).app.daily_time == "08:30"


def test_settings_page_toggles_a_source(qapp, tmp_path):
    from dailywallpaper.config import load_config
    from dailywallpaper.ui.settings_page import SettingsPage

    cfg, path = _config_file(tmp_path)
    page = SettingsPage(cfg, path, autostart=_RecordingAutostart())
    page.wallhaven_check.setChecked(False)

    assert page.save() is True
    assert load_config(path).sources.wallhaven_enabled is False


def test_today_tab_marks_a_reviewed_wallpaper(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,title,created_at)"
                 " values('bing','1','复核图','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-18',?,'70','80','74.5','r','applied','x')", (wid,))
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,created_at)"
        " values('review-model','v1','h','{}',90,70,60,10,'x')")
    aid = conn.execute("select id from analyses").fetchone()[0]
    conn.execute("insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
                 " values(?,?,'x')", (wid, aid))
    conn.commit()

    window = MainWindow(_Service(), conn, review_model="review-model")
    window.refresh()

    assert "已复核" in window.reviewed_label.text()


def test_today_tab_marks_an_unreviewed_wallpaper(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,title,created_at)"
                 " values('bing','1','普通图','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-18',?,'70','80','74.5','r','applied','x')", (wid,))
    conn.commit()

    window = MainWindow(_Service(), conn, review_model="review-model")
    window.refresh()

    assert "已复核" not in window.reviewed_label.text()


def test_today_tab_shows_the_four_sub_scores(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,title,created_at)"
                 " values('bing','1','分数图','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-18',?,'70','80','74.5','r','applied','x')", (wid,))
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,created_at)"
        " values('m','v1','h','{}',93,71,64,18,'x')")
    aid = conn.execute("select id from analyses").fetchone()[0]
    conn.execute("insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
                 " values(?,?,'x')", (wid, aid))
    conn.commit()

    window = MainWindow(_Service(), conn)
    window.refresh()

    assert "93" in window.subscore_label.text()
    assert "18" in window.subscore_label.text()


def test_settings_page_toggles_the_manual_override(qapp, tmp_path):
    from dailywallpaper.config import load_config
    from dailywallpaper.ui.settings_page import SettingsPage

    cfg, path = _config_file(tmp_path)
    page = SettingsPage(cfg, path, autostart=_RecordingAutostart())
    page.manual_override_check.setChecked(True)

    assert page.save() is True
    assert load_config(path).app.allow_manual_override_after_daily_change is True


def test_settings_page_syncs_autostart_with_the_checkbox(qapp, tmp_path):
    from dailywallpaper.ui.settings_page import SettingsPage

    cfg, path = _config_file(tmp_path)
    autostart = _RecordingAutostart()
    page = SettingsPage(cfg, path, autostart=autostart)

    page.autostart_check.setChecked(True)
    page.save()

    assert autostart.calls == [True]


def test_worker_runs_a_manual_request_without_waiting_for_the_interval():
    import time as _time

    from dailywallpaper.ui.worker import Worker

    calls = []

    class _M:
        def tick(self, dry_run=False, manual=False):
            calls.append(manual)

    worker = Worker(lambda: _M(), interval=60)
    worker.start()
    try:
        deadline = _time.time() + 2
        while not calls and _time.time() < deadline:
            _time.sleep(0.01)
        assert calls == [False]
        assert worker.change_now() is True
        deadline = _time.time() + 2
        while len(calls) < 2 and _time.time() < deadline:
            _time.sleep(0.01)
        assert calls == [False, True]
    finally:
        worker.stop()


def test_settings_page_reports_a_write_failure(qapp, tmp_path, monkeypatch):
    from dailywallpaper.ui import settings_page as module

    cfg, path = _config_file(tmp_path)

    def boom(_path, _cfg):
        raise OSError("disk full")

    monkeypatch.setattr(module, "save_config", boom)
    page = module.SettingsPage(cfg, path, autostart=_RecordingAutostart())

    assert page.save() is False
    assert "保存失败" in page.status.text()


def test_change_action_tells_the_user_when_it_is_ignored(qapp):
    class _Busy(_Service):
        def change_now(self):
            return False

    tray = TrayIcon(_Busy(), on_open=lambda: None, on_quit=lambda: None)
    notes = []
    tray.notify = lambda title, message: notes.append(message)

    tray.change_action.trigger()

    assert notes and "忽略" in notes[0]


def test_undo_action_tells_the_user_when_there_is_nothing_to_undo(qapp):
    class _NoUndo(_Service):
        def undo(self):
            return False

    tray = TrayIcon(_NoUndo(), on_open=lambda: None, on_quit=lambda: None)
    notes = []
    tray.notify = lambda title, message: notes.append(message)

    tray.undo_action.trigger()

    assert notes and "没有可撤销" in notes[0]


def test_the_desktop_guard_does_nothing_while_paused(tmp_path):
    from datetime import datetime, timedelta

    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.worker import Worker

    conn = connect(tmp_path / "app.db")
    migrate(conn)

    class _Clock:
        def now(self):
            return datetime.now().astimezone()

    worker = Worker(lambda: object())
    until = (datetime.now().astimezone() + timedelta(days=1)).isoformat(
        timespec="seconds"
    )
    conn.execute("insert into state(key,value) values('paused_until',?)", (until,))
    conn.commit()

    assert worker._paused_state(conn, _Clock()) is True

    conn.execute("delete from state where key='paused_until'")
    conn.commit()
    assert worker._paused_state(conn, _Clock()) is False
