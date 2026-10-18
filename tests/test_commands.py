# tests/test_commands.py
import threading

import pytest
from datetime import datetime

from dailywallpaper.config import AppSection
from dailywallpaper.db import connect, migrate
from dailywallpaper.state.machine import TickResult
from dailywallpaper.ui.commands import CommandService


class _Clock:
    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment


class _Machine:
    def __init__(self):
        self.calls = []

    def tick(self, dry_run=False, manual=False):
        self.calls.append((dry_run, manual))
        return TickResult("applied", "", 1)


class _ApplyService:
    def __init__(self, ok=True):
        self.ok = ok
        self.undos = 0

    def undo_last(self):
        self.undos += 1
        return self.ok


def _wallpaper(conn):
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    return conn.execute("select id from wallpapers").fetchone()[0]


def _setup(tmp_path, moment=datetime(2026, 10, 18, 9, 0), config=None):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    _wallpaper(conn)
    machine = _Machine()
    service = CommandService(
        conn, config or AppSection(), _Clock(moment), machine, _ApplyService()
    )
    return conn, machine, service


def test_not_paused_by_default(tmp_path):
    conn, machine, service = _setup(tmp_path)
    assert service.is_paused() is False
    assert service.snapshot().paused is False


def test_pause_for_one_day(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.pause(1)
    assert service.is_paused() is True
    row = conn.execute("select value from state where key='paused_until'").fetchone()
    assert row[0].startswith("2026-10-19")


def test_pause_forever_stores_a_far_future_date(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.pause(None)
    row = conn.execute("select value from state where key='paused_until'").fetchone()
    assert int(row[0][:4]) >= 2099


def test_resume_clears_the_pause(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.pause(7)
    service.resume()
    assert service.is_paused() is False


def test_expired_pause_is_not_paused(tmp_path):
    conn, machine, service = _setup(tmp_path)
    conn.execute("insert into state(key,value) values('paused_until','2026-10-01T00:00:00')")
    conn.commit()
    assert service.is_paused() is False


def test_manual_change_is_allowed_when_nothing_changed_today(tmp_path):
    conn, machine, service = _setup(tmp_path)
    assert service.can_change_manually() is True


def test_manual_change_is_blocked_after_a_success_today(tmp_path):
    conn, machine, service = _setup(tmp_path)
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,method,applied_at,success)"
        " values('2026-10-18',1,'static','x',1)"
    )
    conn.commit()
    assert service.can_change_manually() is False


def test_override_setting_allows_a_second_change(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    _wallpaper(conn)
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,method,applied_at,success)"
        " values('2026-10-18',1,'static','x',1)"
    )
    conn.commit()
    config = AppSection(allow_manual_override_after_daily_change=True)
    service = CommandService(
        conn, config, _Clock(datetime(2026, 10, 18, 9, 0)), _Machine(), _ApplyService()
    )
    assert service.can_change_manually() is True


def test_change_now_asks_the_machine_for_a_real_apply(tmp_path):
    conn, machine, service = _setup(tmp_path)
    result = service.change_now()
    assert result.action == "applied"
    assert machine.calls == [(False, True)]


def test_snapshot_reports_the_last_action(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.change_now()
    snapshot = service.snapshot()
    assert snapshot.last_action == "applied"
    assert snapshot.today_changed is False


def test_undo_delegates_to_the_apply_service(tmp_path):
    conn, machine, service = _setup(tmp_path)
    assert service.undo() is True


def test_a_second_click_while_applying_is_ignored(tmp_path):
    import time

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    _wallpaper(conn)
    release = threading.Event()

    class _SlowMachine:
        def __init__(self):
            self.calls = 0

        def tick(self, dry_run=False, manual=False):
            self.calls += 1
            release.wait(2)
            return TickResult("applied")

    machine = _SlowMachine()
    service = CommandService(
        conn, AppSection(), _Clock(datetime(2026, 10, 18, 9, 0)), machine, _ApplyService()
    )

    first = threading.Thread(target=service.change_now)
    first.start()
    time.sleep(0.05)
    second = service.change_now()
    release.set()
    first.join(2)

    assert machine.calls == 1
    assert second is None


def test_manual_change_still_works_while_paused(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.pause(1)

    service.change_now()

    assert machine.calls == [(False, True)]


def test_manual_change_bypasses_pause_with_the_real_machine(tmp_path):
    from dailywallpaper.models import Decision
    from dailywallpaper.state.machine import DailyStateMachine
    from dailywallpaper.analysis.vision import VisionScores

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    wid = _wallpaper(conn)

    class _Pipeline:
        def prepare(self, now):
            pass

        def pick(self, now):
            return Decision(
                wid, __import__("pathlib").Path("x.jpg"),
                VisionScores(90, 70, 60, 10, False, False, "none",
                             ["landscape"], ["calm"], ["#112233"], "s", "r"),
                60.0, 60.0, 60.0, "pick",
            )

    class _Apply:
        def __init__(self):
            self.triggers = []

        def apply(self, wallpaper_id, image_path, trigger="auto"):
            self.triggers.append(trigger)
            return True

        def undo_last(self):
            return True

    apply_service = _Apply()
    machine = DailyStateMachine(
        conn, AppSection(), _Clock(datetime(2026, 10, 18, 23, 30)), _Pipeline(), apply_service
    )
    service = CommandService(conn, AppSection(), machine.clock, machine, apply_service)

    service.pause(7)
    result = service.change_now()

    assert result.action == "applied"
    assert apply_service.triggers == ["manual"]


def test_pause_zero_days_is_rejected(tmp_path):
    conn, machine, service = _setup(tmp_path)

    with pytest.raises(ValueError):
        service.pause(0)
