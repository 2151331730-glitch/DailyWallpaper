# tests/test_state.py
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from dailywallpaper.analysis.vision import VisionScores
from dailywallpaper.config import AppSection
from dailywallpaper.db import connect, migrate
from dailywallpaper.models import Decision
from dailywallpaper.state.machine import PREPARED_KEY, DailyStateMachine


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

    def prepare(self, now, force=False, mark=True, dry_run=False):
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


def test_a_deferred_apply_is_not_recorded_as_a_failure(tmp_path):
    """A game taking the screen must not cost the pick its good name."""
    conn, machine, clock, pipeline, svc = _setup(
        tmp_path, datetime(2026, 10, 18, 9, 0)
    )
    failed = []
    pipeline.mark_failed = failed.append
    pipeline.mark_applied = lambda decision: None
    svc.last_deferred = True
    svc.apply = lambda *args, **kwargs: False

    result = machine.tick()

    assert result.action == "deferred"
    assert failed == []


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



def _exhausted_pipeline(decision, calls):
    class _Pipeline:
        def __init__(self):
            self.calls = 0

        def prepare(self, now, force=False, mark=True, dry_run=False):
            calls.append("prepare")

        def prepare_local(self, now):
            calls.append("prepare_local")
            return True

        def pick(self, now, dry_run=False):
            self.calls += 1
            return None if self.calls == 1 else decision

    return _Pipeline()


def test_manual_change_refills_from_the_disk_not_the_network(tmp_path):
    """A click has to be answered now; a network rebuild answered in 34-65 s."""
    from types import SimpleNamespace

    from dailywallpaper.config import AppSection
    from dailywallpaper.state.machine import DailyStateMachine

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    clock = FakeClock(datetime(2026, 10, 18, 9, 0))
    calls = []
    machine = DailyStateMachine(
        conn,
        SimpleNamespace(app=AppSection()),
        clock,
        _exhausted_pipeline(_decision(wid), calls),
        FakeApplyService(conn, clock),
    )

    result = machine.tick(manual=True)

    assert result.action == "applied"
    assert calls == ["prepare_local"]


def test_a_manual_change_answers_from_history_instead_of_waiting(tmp_path):
    # A refill takes 40-60 s and can fail outright; the click gets a known
    # good wallpaper at once rather than looking dead for a minute.
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
    repeated = _decision(wid)

    class Pipeline:
        def prepare(self, now, force=False, mark=True, dry_run=False):
            forces.append(force)

        def prepare_local(self, now):
            forces.append("local")

        def pick(self, now, dry_run=False):
            return None

        def repeat_pick(self, now):
            return repeated

    machine = DailyStateMachine(
        conn,
        SimpleNamespace(app=AppSection()),
        clock,
        Pipeline(),
        FakeApplyService(conn, clock),
    )

    result = machine.tick(manual=True)

    assert result.action == "applied"
    # Only the disk rebuild ran; no network pass sat in front of the click.
    assert forces == ["local"]


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
    # The scheduled pass keeps its network refill; only a click is disk-only.
    assert forces == ["prepare"]


# -- pre-generated wallpaper ---------------------------------------------
def _park_prepared(conn, tmp_path, wid, decision_id=None, media_kind="image"):
    image = tmp_path / f"{wid}.jpg"
    image.write_bytes(b"jpeg")
    payload = {
        "wallpaper_id": wid,
        "apply_path": str(image),
        "image_path": str(image),
        "decision_id": decision_id,
        "media_kind": media_kind,
        "reason": "parked pick",
        "created_at": "2026-10-17T22:00:00",
    }
    conn.execute(
        "insert or replace into state(key,value) values(?,?)",
        (PREPARED_KEY, json.dumps(payload)),
    )
    conn.commit()
    return image


def test_startup_applies_the_prepared_wallpaper_before_daily_time(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 7, 0))
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-17',?,'x',1,1,1,'shortlisted','x')",
        (wid,),
    )
    conn.commit()
    decision_id = conn.execute("select max(id) from decisions").fetchone()[0]
    _park_prepared(conn, tmp_path, wid, decision_id)

    result = machine.tick(startup=True)

    assert result.action == "applied"
    assert svc.calls == [(wid, "auto")]
    assert conn.execute(
        "select 1 from state where key=?", (PREPARED_KEY,)
    ).fetchone() is None
    assert conn.execute(
        "select status from decisions where id=?", (decision_id,)
    ).fetchone()[0] == "applied"
    # The slow pipeline never ran: the parked file was already inspected.
    assert pipeline.prepared == 0


def test_prepared_wallpaper_waits_outside_the_daily_window(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 7, 0))
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    _park_prepared(conn, tmp_path, wid)

    result = machine.tick()

    assert result.action == "background"
    assert svc.calls == []
    assert conn.execute(
        "select 1 from state where key=?", (PREPARED_KEY,)
    ).fetchone() is not None


def test_startup_does_not_apply_twice_in_one_day(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 7, 0))
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    _park_prepared(conn, tmp_path, wid)
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,method,applied_at,success)"
        " values('2026-10-18',1,'static','x',1)"
    )
    conn.commit()

    assert machine.tick(startup=True).action == "already_changed"
    assert svc.calls == []


def test_missing_prepared_file_falls_back_to_the_pipeline(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    image = tmp_path / "gone.jpg"
    conn.execute(
        "insert or replace into state(key,value) values(?,?)",
        (
            PREPARED_KEY,
            json.dumps({"wallpaper_id": wid, "apply_path": str(image)}),
        ),
    )
    conn.commit()

    result = machine.tick(startup=True)

    assert result.action == "applied"
    assert [call[0] for call in svc.calls] == [wid]


def test_prepared_wallpaper_rated_one_is_dropped(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 7, 0))
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    _park_prepared(conn, tmp_path, wid)
    conn.execute(
        "insert into ratings(wallpaper_id,rating,created_at) values(?,1,'x')", (wid,)
    )
    conn.commit()

    result = machine.tick(startup=True)

    # 07:00 without a usable parked pick is the old background warm-up.
    assert result.action == "background"
    assert svc.calls == []
    assert conn.execute(
        "select 1 from state where key=?", (PREPARED_KEY,)
    ).fetchone() is None


def test_parked_wallpaper_survives_days_without_booting(tmp_path):
    # "整天没开机" must not throw the pick away: the machine may skip a day,
    # so the parked file is used whenever the next boot finally happens.
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 7, 0))
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    _park_prepared(conn, tmp_path, wid)

    clock.advance(days=3)

    result = machine.tick(startup=True)

    assert result.action == "applied"
    assert [call[0] for call in svc.calls] == [wid]


def test_dry_run_never_consumes_the_parked_wallpaper(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    _park_prepared(conn, tmp_path, wid)

    result = machine.tick(dry_run=True)

    assert result.action == "dry_run"
    assert svc.calls == []
    assert conn.execute(
        "select 1 from state where key=?", (PREPARED_KEY,)
    ).fetchone() is not None


class _Power:
    def __init__(self, on_battery):
        self._on_battery = on_battery

    def on_battery(self):
        return self._on_battery


def test_parked_live_wallpaper_is_dropped_on_battery(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 7, 0))
    machine.config = SimpleNamespace(
        app=AppSection(), wallpaper=SimpleNamespace(dynamic_requires_ac=True)
    )
    machine.power = _Power(on_battery=True)
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    _park_prepared(conn, tmp_path, wid, media_kind="scene")

    result = machine.tick(startup=True)

    # 07:00 with nothing usable is the ordinary background warm-up.
    assert result.action == "background"
    assert svc.calls == []
    assert conn.execute(
        "select 1 from state where key=?", (PREPARED_KEY,)
    ).fetchone() is None


def test_parked_live_wallpaper_survives_on_ac(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 7, 0))
    machine.config = SimpleNamespace(
        app=AppSection(), wallpaper=SimpleNamespace(dynamic_requires_ac=True)
    )
    machine.power = _Power(on_battery=False)
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    _park_prepared(conn, tmp_path, wid, media_kind="scene")

    assert machine.tick(startup=True).action == "applied"
    assert svc.calls == [(wid, "auto")]


def test_a_dry_run_does_not_touch_the_desktop(tmp_path):
    conn, machine, clock, pipeline, svc = _setup(tmp_path, datetime(2026, 10, 18, 9, 0))

    machine.tick(dry_run=True)

    assert svc.calls == []
