# tests/test_state.py
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from dailywallpaper.analysis.vision import VisionScores
from dailywallpaper.config import AppSection
from dailywallpaper.db import connect, migrate
from dailywallpaper.models import Decision
from dailywallpaper.state.machine import DailyStateMachine


class FakeClock:
    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment

    def advance(self, days=0, hours=0):
        self.moment += timedelta(days=days, hours=hours)


class FakePipeline:
    def __init__(self, decision):
        self.decision = decision
        self.prepared = 0

    def prepare(self, now, force=False, mark=True):
        self.prepared += 1

    def pick(self, now, dry_run=False):
        return self.decision


class FakeApplyService:
    def __init__(self, conn, clock):
        self.conn = conn
        self.clock = clock
        self.calls = []

    def apply(self, wallpaper_id, image_path, trigger="auto"):
        self.calls.append((wallpaper_id, trigger))
        self.conn.execute(
            "insert into change_log(change_date,wallpaper_id,method,applied_at,success)"
            " values(?,?,?,?,1)",
            (
                self.clock.now().date().isoformat(),
                wallpaper_id,
                "manual" if trigger == "manual" else "static",
                "x",
            ),
        )
        self.conn.commit()
        return True


def _scores():
    return VisionScores(
        quality=80, creativity=70, novelty=60, formulaic=20, nsfw=False,
        watermark=False, text_overlay="none", style_tags=["landscape"],
        mood_tags=["calm"], palette=["#112233"], subject="s", reason="r",
    )


def _decision(wid):
    return Decision(
        wallpaper_id=wid, image_path=Path("x.jpg"), scores=_scores(),
        explore=60.0, personalize=60.0, final=60.0, reason="pick",
    )


def _setup(tmp_path, moment, decision=None, app=None):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    clock = FakeClock(moment)
    pipeline = FakePipeline(_decision(wid) if decision is None else decision)
    apply_service = FakeApplyService(conn, clock)
    config = SimpleNamespace(app=app or AppSection())
    machine = DailyStateMachine(conn, config, clock, pipeline, apply_service)
    return conn, machine, clock, pipeline, apply_service


def test_before_daily_time_runs_in_background(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 7, 0))

    result = machine.tick()

    assert result.action == "background"
    assert pipeline.prepared == 1
    assert svc.calls == []


def test_after_latest_change_time_is_too_late(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 23, 0))

    assert machine.tick().action == "too_late"
    assert svc.calls == []


def test_already_changed_today_is_skipped(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,method,applied_at,success)"
        " values('2026-10-18',1,'static','x',1)"
    )
    conn.commit()

    assert machine.tick().action == "already_changed"
    assert svc.calls == []


def test_paused_short_circuits(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))
    conn.execute("insert into state(key,value) values('paused_until','2026-10-19T00:00:00')")
    conn.commit()

    assert machine.tick().action == "paused"
    assert svc.calls == []


def test_paused_with_a_naive_stored_timestamp_does_not_crash(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))
    # now_iso() writes a naive timestamp; the system clock is aware.
    conn.execute(
        "insert into state(key,value) values('paused_until','2099-01-01T00:00:00')"
    )
    conn.commit()
    machine.clock = type("AwareClock", (), {"now": lambda self: datetime.now().astimezone()})()

    assert machine.tick().action == "paused"


def test_malformed_paused_until_degrades_to_not_paused(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))
    conn.execute("insert into state(key,value) values('paused_until','not-a-date')")
    conn.commit()

    assert machine.tick().action == "applied"


def test_candidate_is_applied_once(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))

    result = machine.tick()

    assert result.action == "applied"
    assert len(svc.calls) == 1
    assert svc.calls[0][1] == "auto"


def test_dry_run_does_not_apply(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))

    result = machine.tick(dry_run=True)

    assert result.action == "dry_run"
    assert svc.calls == []


def test_no_candidate_keeps_the_current_wallpaper(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(
        tmp_path, datetime(2026, 10, 18, 9, 0), decision=None
    )
    pipeline.decision = None

    result = machine.tick()

    assert result.action == "kept"
    assert svc.calls == []
    assert pipeline.prepared == 1


def test_seven_day_loop_writes_one_success_per_day(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))

    for _ in range(7):
        assert machine.tick().action == "applied"
        clock.advance(days=1)

    rows = conn.execute("select change_date, success from change_log").fetchall()
    assert len(rows) == 7
    assert all(row["success"] == 1 for row in rows)
    assert len({row["change_date"] for row in rows}) == 7


def test_malformed_daily_time_does_not_crash(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(
        tmp_path, datetime(2026, 10, 18, 9, 0), app=AppSection(daily_time="25:99")
    )

    assert machine.tick().action in {"applied", "kept", "background", "too_late"}


def test_malformed_latest_change_time_does_not_crash(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(
        tmp_path, datetime(2026, 10, 18, 9, 0), app=AppSection(latest_change_time="oops")
    )

    assert machine.tick().action in {"applied", "kept", "background", "too_late"}



def _exhausted_pipeline(decision, forces):
    class _Pipeline:
        def __init__(self):
            self.calls = 0

        def prepare(self, now, force=False, mark=True):
            forces.append(force)

        def pick(self, now, dry_run=False):
            self.calls += 1
            return None if self.calls == 1 else decision

    return _Pipeline()


def test_manual_change_refills_an_exhausted_shortlist(tmp_path):
    from types import SimpleNamespace

    from dailywallpaper.config import AppSection
    from dailywallpaper.state.machine import DailyStateMachine

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    clock = FakeClock(datetime(2026, 10, 18, 9, 0))
    forces = []
    machine = DailyStateMachine(
        conn,
        SimpleNamespace(app=AppSection()),
        clock,
        _exhausted_pipeline(_decision(wid), forces),
        FakeApplyService(conn, clock),
    )

    result = machine.tick(manual=True)

    assert result.action == "applied"
    assert forces == [True]


def test_automatic_change_refills_without_forcing(tmp_path):
    from types import SimpleNamespace

    from dailywallpaper.config import AppSection
    from dailywallpaper.state.machine import DailyStateMachine

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    clock = FakeClock(datetime(2026, 10, 18, 9, 0))
    forces = []
    machine = DailyStateMachine(
        conn,
        SimpleNamespace(app=AppSection()),
        clock,
        _exhausted_pipeline(_decision(wid), forces),
        FakeApplyService(conn, clock),
    )

    result = machine.tick()

    assert result.action == "applied"
    assert forces == [False]
