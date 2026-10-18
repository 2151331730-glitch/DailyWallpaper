# tests/test_apply.py
from pathlib import Path

from dailywallpaper.apply.service import ApplyService
from dailywallpaper.apply.windows_static import ApplyResult, WallpaperState
from dailywallpaper.db import connect, migrate


class _FakeApplier:
    def __init__(self, ok=True, wallpaper="C:/old.jpg"):
        self.ok = ok
        self.current = WallpaperState(wallpaper=wallpaper, style="22", tile="0")
        self.restored = []
        self.applied = []

    def snapshot(self):
        return self.current

    def apply(self, image, style="fill"):
        self.applied.append(image)
        if self.ok:
            self.current = WallpaperState(wallpaper=str(image), style="22", tile="0")
            return ApplyResult(ok=True)
        return ApplyResult(ok=False, error="boom")

    def restore(self, state):
        self.restored.append(state)
        self.current = state


class _FakeWe:
    def __init__(self, current="D:/we/scene.pkg", close_ok=True):
        self._current = current
        self.close_ok = close_ok
        self.closed = 0
        self.opened = []

    def current(self):
        return self._current

    def close(self):
        self.closed += 1
        return self.close_ok

    def open(self, file, monitor=0):
        self.opened.append(str(file).replace("\\", "/"))
        return True


def _wallpaper(conn):
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    return conn.execute("select id from wallpapers").fetchone()[0]


def _service(tmp_path, ok=True, we=None):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    old = tmp_path / "old.jpg"
    old.write_bytes(b"x")
    return conn, _wallpaper(conn), ApplyService(
        conn, _FakeApplier(ok=ok, wallpaper=str(old)), we=we
    )


def test_successful_apply_logs_a_static_change(tmp_path):
    conn, wid, service = _service(tmp_path)

    assert service.apply(wid, tmp_path / "new.jpg") is True

    row = conn.execute("select * from change_log").fetchone()
    assert row["success"] == 1
    assert row["method"] == "static"
    assert row["previous_wallpaper"].endswith("old.jpg")


def test_failed_apply_writes_no_success_row(tmp_path):
    conn, wid, service = _service(tmp_path, ok=False)

    assert service.apply(wid, tmp_path / "new.jpg") is False

    assert conn.execute("select count(*) from change_log").fetchone()[0] == 0


def test_manual_trigger_is_recorded_as_manual(tmp_path):
    conn, wid, service = _service(tmp_path)

    assert service.apply(wid, tmp_path / "new.jpg", trigger="manual") is True

    assert conn.execute("select method from change_log").fetchone()[0] == "manual"


def test_we_is_closed_before_the_static_apply(tmp_path):
    we = _FakeWe()
    conn, wid, service = _service(tmp_path, we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is True
    assert we.closed == 1


def test_we_is_reopened_when_the_apply_fails(tmp_path):
    we = _FakeWe()
    conn, wid, service = _service(tmp_path, ok=False, we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is False
    assert we.opened == ["D:/we/scene.pkg"]


def test_apply_aborts_when_we_cannot_close(tmp_path):
    we = _FakeWe(close_ok=False)
    conn, wid, service = _service(tmp_path, we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is False
    assert conn.execute("select count(*) from change_log").fetchone()[0] == 0


def test_undo_restores_the_previous_wallpaper(tmp_path):
    conn, wid, service = _service(tmp_path)
    service.apply(wid, tmp_path / "new.jpg")

    assert service.undo_last() is True

    assert service.applier.restored[-1].wallpaper.endswith("old.jpg")


def test_change_date_follows_an_injected_clock(tmp_path):
    from datetime import datetime

    class _Clock:
        def now(self):
            return datetime(2026, 10, 20, 9, 0)

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    service = ApplyService(conn, _FakeApplier(), clock=_Clock())

    service.apply(wid, tmp_path / "new.jpg")

    assert conn.execute("select change_date from change_log").fetchone()[0] == "2026-10-20"


def test_undo_without_history_returns_false(tmp_path):
    conn, wid, service = _service(tmp_path)

    assert service.undo_last() is False


def test_undo_reopens_the_previous_we_project(tmp_path):
    we = _FakeWe()
    conn, wid, service = _service(tmp_path, we=we)
    service.apply(wid, tmp_path / "new.jpg")

    assert service.undo_last() is True
    assert we.opened == ["D:/we/scene.pkg"]


def test_undo_refuses_a_missing_previous_wallpaper(tmp_path):
    conn, wid, service = _service(tmp_path)
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,previous_wallpaper,method,applied_at,success)"
        " values('2026-10-18',1,'C:/does/not/exist.jpg','static','x',1)"
    )
    conn.commit()

    assert service.undo_last() is False


def test_applier_exception_is_treated_as_failure(tmp_path):
    class _Boom(_FakeApplier):
        def apply(self, image, style="fill"):
            raise RuntimeError("boom")

    we = _FakeWe()
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    service = ApplyService(conn, _Boom(), we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is False
    assert we.opened == ["D:/we/scene.pkg"]
    assert conn.execute("select count(*) from change_log").fetchone()[0] == 0


def test_a_dynamic_wallpaper_uses_the_we_applier(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,media_type,created_at)"
                 " values('we_workshop','1','video','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    static, dynamic = _FakeApplier(), _FakeApplier()
    service = ApplyService(conn, static, dynamic_applier=dynamic)

    assert service.apply(wid, tmp_path / "clip.mp4") is True
    assert static.restored == [] and dynamic.restored == []
    row = conn.execute("select method from change_log").fetchone()
    assert row[0] == "we"


def test_a_real_we_applier_works_through_the_service(tmp_path):
    from dailywallpaper.apply.we_applier import WeApplier

    project = tmp_path / "111" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")

    class Control:
        def __init__(self):
            self.current_value = "D:/we/old/project.json"
            self.opened = []

        def current(self):
            return self.current_value

        def open(self, file, monitor=0):
            self.opened.append(Path(file))
            self.current_value = str(Path(file).parent / "scene.pkg")
            return True

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,media_type,created_at)"
                 " values('we_workshop','111','scene','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    service = ApplyService(
        conn, _FakeApplier(), dynamic_applier=WeApplier(Control(), sleep=lambda s: None)
    )

    assert service.apply(wid, project) is True

    row = conn.execute("select method, previous_wallpaper, success from change_log").fetchone()
    assert row["method"] == "we"
    assert row["success"] == 1
    assert row["previous_wallpaper"] == "D:/we/old/project.json"


def test_a_static_apply_releases_wallpaper_engine(tmp_path):
    we = _FakeWe()
    conn, wid, service = _service(tmp_path, we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is True
    assert we.closed == 1


def test_a_dynamic_apply_does_not_close_wallpaper_engine_first(tmp_path):
    from dailywallpaper.apply.we_applier import WeApplier

    class Control:
        def __init__(self):
            self.current_value = "D:/we/old/project.json"

        def current(self):
            return self.current_value

        def open(self, file, monitor=0):
            self.current_value = str(Path(file).parent / "scene.pkg")
            return True

        def close(self):
            raise AssertionError("must not close WE before opening a WE project")

    project = tmp_path / "111" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,media_type,created_at)"
                 " values('we_workshop','1','scene','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    service = ApplyService(
        conn, _FakeApplier(), we=Control(), dynamic_applier=WeApplier(Control(), sleep=lambda s: None)
    )

    assert service.apply(wid, project) is True


def _recheck_service(tmp_path, we, delays=(5, 30)):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    old = tmp_path / "old.jpg"
    old.write_bytes(b"x")
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    slept = []
    service = ApplyService(
        conn,
        _FakeApplier(wallpaper=str(old)),
        we=we,
        recheck_delays=delays,
        sleep=slept.append,
        spawn=lambda target: target(),
    )
    return service, wid, slept


def test_a_static_apply_rechecks_for_a_we_takeover_at_5s_and_30s(tmp_path):
    we = _FakeWe()
    service, wid, slept = _recheck_service(tmp_path, we)

    assert service.apply(wid, tmp_path / "new.jpg") is True

    assert slept == [5, 25]
    # Once before the static apply, then once per recheck.
    assert we.closed == 3
    assert len(service.applier.applied) == 3


def test_no_recheck_happens_when_we_has_no_wallpaper(tmp_path):
    we = _FakeWe(current="")
    service, wid, slept = _recheck_service(tmp_path, we)

    assert service.apply(wid, tmp_path / "new.jpg") is True

    assert slept == [5, 25]
    assert we.closed == 0
    assert len(service.applier.applied) == 1


def test_a_dynamic_apply_is_not_rechecked(tmp_path):
    from dailywallpaper.apply.we_applier import WeApplier

    class Control:
        def __init__(self):
            self.current_value = "D:/we/old/project.json"

        def current(self):
            return self.current_value

        def open(self, file, monitor=0):
            self.current_value = str(Path(file).parent / "scene.pkg")
            return True

    project = tmp_path / "111" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,media_type,created_at)"
                 " values('we_workshop','111','scene','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    control = Control()
    slept = []
    service = ApplyService(
        conn,
        _FakeApplier(),
        we=control,
        dynamic_applier=WeApplier(control, sleep=lambda s: None),
        recheck_delays=(5, 30),
        sleep=slept.append,
        spawn=lambda target: target(),
    )

    assert service.apply(wid, project) is True
    assert slept == []


def test_a_failing_recheck_is_swallowed(tmp_path):
    class Flaky(_FakeApplier):
        def __init__(self, fail_from=1):
            super().__init__()
            self.calls = 0
            self.fail_from = fail_from

        def apply(self, image, style="fill"):
            self.calls += 1
            if self.calls > self.fail_from:
                raise OSError("WE came back mid-recheck")
            return super().apply(image, style)

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    service = ApplyService(
        conn,
        Flaky(),
        we=_FakeWe(),
        recheck_delays=(5, 30),
        sleep=lambda s: None,
        spawn=lambda target: target(),
    )

    assert service.apply(wid, tmp_path / "new.jpg") is True
    assert conn.execute("select count(*) from change_log").fetchone()[0] == 1


def _logged_wallpaper(conn, image_path, media_type="image"):
    conn.execute(
        "insert into wallpapers(source,source_id,local_path,media_type,created_at)"
        " values('t','1',?,'?','x')".replace("'?'", "?"),
        (str(image_path), media_type),
    )
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,previous_wallpaper,"
        "previous_we_config_json,method,applied_at,success) values('2026-10-18',?,?,?,?,?,1)",
        (wid, "", "{}", "static", "2026-10-18T18:50:38"),
    )
    conn.commit()
    return wid


def test_startup_reapplies_the_last_static_wallpaper_over_we(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = tmp_path / "wall.jpg"
    image.write_bytes(b"x")
    _logged_wallpaper(conn, image)
    we = _FakeWe()
    service = ApplyService(conn, _FakeApplier(), we=we)

    assert service.ensure_visible() is True
    assert we.closed == 1
    assert service.applier.applied[-1] == image


def test_startup_leaves_a_dynamic_wallpaper_alone(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = tmp_path / "scene.pkg"
    image.write_bytes(b"x")
    _logged_wallpaper(conn, image, media_type="scene")
    we = _FakeWe()
    service = ApplyService(conn, _FakeApplier(), we=we)

    assert service.ensure_visible() is False
    assert we.closed == 0
    assert service.applier.applied == []


def test_startup_without_history_does_nothing(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    we = _FakeWe()
    service = ApplyService(conn, _FakeApplier(), we=we)

    assert service.ensure_visible() is False
    assert we.closed == 0


def test_startup_failure_is_reported_not_raised(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = tmp_path / "wall.jpg"
    image.write_bytes(b"x")
    _logged_wallpaper(conn, image)
    service = ApplyService(conn, _FakeApplier(ok=False), we=_FakeWe())

    assert service.ensure_visible() is False


def test_an_undo_cancels_a_pending_recheck(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    old = tmp_path / "old.jpg"
    old.write_bytes(b"x")
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    targets = []
    service = ApplyService(
        conn, _FakeApplier(wallpaper=str(old)), we=_FakeWe(),
        recheck_delays=(5, 30), sleep=lambda s: None, spawn=targets.append,
    )

    assert service.apply(wid, tmp_path / "new.jpg") is True
    assert service.undo_last() is True
    applied_before = len(service.applier.applied)

    targets[0]()

    assert len(service.applier.applied) == applied_before


def test_the_recheck_leaves_a_desktop_that_still_shows_our_image(tmp_path):
    class _Aware(_FakeApplier):
        def __init__(self, target):
            super().__init__()
            self.target = target

        def current_wallpaper(self):
            return str(self.target.with_suffix(".apply.jpg"))

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    target = tmp_path / "new.jpg"
    applier = _Aware(target)
    service = ApplyService(
        conn, applier, we=_FakeWe(),
        recheck_delays=(5, 30), sleep=lambda s: None, spawn=lambda t: t(),
    )

    assert service.apply(wid, target) is True
    assert len(applier.applied) == 1


def test_startup_does_not_restore_an_undone_wallpaper(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = tmp_path / "wall.jpg"
    image.write_bytes(b"x")
    previous = tmp_path / "previous.jpg"
    previous.write_bytes(b"x")
    conn.execute(
        "insert into wallpapers(source,source_id,local_path,media_type,created_at)"
        " values('t','1',?,'image','x')",
        (str(image),),
    )
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,previous_wallpaper,"
        "previous_we_config_json,method,applied_at,success)"
        " values('2026-10-18',?,?,'{}','static','2020-01-01T00:00:00',1)",
        (wid, str(previous)),
    )
    conn.commit()
    service = ApplyService(conn, _FakeApplier(wallpaper=str(previous)))

    assert service.undo_last() is True
    assert service.ensure_visible() is False


def test_startup_applies_even_when_we_cannot_be_closed(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = tmp_path / "wall.jpg"
    image.write_bytes(b"x")
    _logged_wallpaper(conn, image)
    service = ApplyService(conn, _FakeApplier(), we=_FakeWe(close_ok=False))

    assert service.ensure_visible() is True
    assert service.applier.applied[-1] == image


def test_a_successful_apply_records_the_applied_path(tmp_path):
    conn, wid, service = _service(tmp_path)
    target = tmp_path / "new.jpg"

    assert service.apply(wid, target) is True

    row = conn.execute("select value from state where key='last_applied_path'").fetchone()
    assert row[0] == str(target)


def test_startup_prefers_the_recorded_applied_path(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    preview = tmp_path / "preview.jpg"
    preview.write_bytes(b"x")
    full = tmp_path / "full.jpg"
    full.write_bytes(b"x")
    _logged_wallpaper(conn, preview)
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(full),),
    )
    conn.commit()
    service = ApplyService(conn, _FakeApplier())

    assert service.ensure_visible() is True
    assert service.applier.applied[-1] == full


class _BrokenWe:
    def current(self):
        raise FileNotFoundError("config.json")

    def close(self):
        raise FileNotFoundError("config.json")

    def open(self, file, monitor=0):
        raise FileNotFoundError("config.json")


def test_a_broken_we_config_does_not_block_a_static_apply(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    service = ApplyService(conn, _FakeApplier(), we=_BrokenWe())

    assert service.apply(wid, tmp_path / "new.jpg") is True
    assert conn.execute("select count(*) from change_log where success=1").fetchone()[0] == 1


def test_an_undo_in_another_service_cancels_the_recheck(tmp_path):
    conn_a = connect(tmp_path / "app.db")
    migrate(conn_a)
    conn_a.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn_a.commit()
    wid = conn_a.execute("select id from wallpapers").fetchone()[0]
    conn_b = connect(tmp_path / "app.db")
    targets = []
    service_a = ApplyService(
        conn_a, _FakeApplier(wallpaper="C:/old.jpg"), we=_FakeWe(),
        recheck_delays=(5, 30), sleep=lambda s: None, spawn=targets.append,
    )
    assert service_a.apply(wid, tmp_path / "new.jpg") is True
    service_b = ApplyService(conn_b, _FakeApplier(wallpaper="C:/old.jpg"), we=_FakeWe())
    service_b.undo_last()
    applied_before = len(service_a.applier.applied)

    targets[0]()

    assert len(service_a.applier.applied) == applied_before
