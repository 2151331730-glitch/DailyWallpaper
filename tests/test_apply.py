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
            # WindowsStaticApplier applies the ".apply.jpg" sibling, and that
            # is what the desktop reports back -- a fake that kept the original
            # path made the "already black" check look correct when it never
            # matched in production.
            applied = Path(image).with_suffix(".apply.jpg")
            self.current = WallpaperState(wallpaper=str(applied), style="22", tile="0")
            return ApplyResult(ok=True)
        return ApplyResult(ok=False, error="boom")

    def restore(self, state):
        self.restored.append(state)
        self.current = state

    def current_wallpaper(self):
        return self.current.wallpaper


class _FakeWe:
    def __init__(self, current="D:/we/scene.pkg", close_ok=True):
        self._current = current
        self.rendering_now = bool(current)
        self.close_ok = close_ok
        self.closed = 0
        self.opened = []
        self.replaced = []
        self.replaced_monitors = []
        self.sound_calls = []

    def current(self):
        return self._current

    def rendering(self):
        return self.rendering_now

    def close(self):
        self.closed += 1
        self.rendering_now = False
        return self.close_ok

    def open(self, file, monitor=0):
        self.opened.append(str(file).replace("\\", "/"))
        return True

    def replace_selection(self, file, monitor=None):
        self.replaced.append(str(file).replace("\\", "/"))
        self.replaced_monitors.append(monitor)
        return True

    def mute(self):
        self.sound_calls.append("mute")
        return True

    def unmute(self):
        self.sound_calls.append("unmute")
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


def test_we_is_closed_after_the_static_picture_is_in_place(tmp_path):
    # Closing first left the old Windows wallpaper showing in the gap before
    # the new picture landed -- that gap is the flash on the way to a still.
    we = _FakeWe()
    conn, wid, service = _service(tmp_path, we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is True
    assert we.closed == 1


def test_a_failed_static_apply_never_closes_we(tmp_path):
    we = _FakeWe()
    conn, wid, service = _service(tmp_path, ok=False, we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is False
    assert we.closed == 0
    assert we.opened == []


def test_a_failed_we_close_does_not_undo_the_new_picture(tmp_path):
    # The picture is already on the desktop underneath; a WE that refuses to
    # close is the guard's problem, not a failed change.
    we = _FakeWe(close_ok=False)
    conn, wid, service = _service(tmp_path, we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is True
    assert conn.execute("select count(*) from change_log").fetchone()[0] == 1


def test_a_we_that_is_up_but_not_drawing_is_still_closed(tmp_path):
    # Its desktop window can go unrecognised while it still owns the screen;
    # asking "is it rendering" then skips the close, and the still picture
    # only appears when the guard gets to it ten seconds later.
    we = _FakeWe()
    we.rendering_now = False
    we.running = lambda: True
    conn, wid, service = _service(tmp_path, we=we)

    assert service.apply(wid, tmp_path / "new.jpg") is True
    assert we.closed == 1


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
    # WE is closed only after a successful static apply, so a raised applier
    # never took it off the screen -- reopening it here would be a no-op at
    # best and a bogus openWallpaper(".") when we_before is empty.
    assert we.opened == []
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


class _ComeBackWe(_FakeWe):
    """WE that is closed and immediately takes the desktop again.

    What the post-apply recheck exists for: the Windows wallpaper still names
    our picture while WE draws over it, so only WE's own rendering state says
    a takeover happened.
    """

    def close(self):
        self.closed += 1
        return self.close_ok


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
    we = _ComeBackWe()
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
    # No static recheck delays (5, 30); the black beat restores on its own
    # schedule and is not a recheck.
    assert not any(delay in (5, 30) for delay in slept)


def test_a_live_swap_paints_the_desktop_black_first(tmp_path):
    # Between two live wallpapers WE unloads one before the other is ready.
    # The desktop is painted black first so that gap is a black beat instead
    # of a flash of the old still picture.
    from dailywallpaper.apply.we_applier import WeApplier

    project = tmp_path / "222" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,created_at)"
        " values('we_workshop','222','scene','x')"
    )
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    control = _FakeWe(current="D:/we/old/scene.pkg")

    def open_and_resolve(file, monitor=0):
        control.opened.append(str(file).replace("\\", "/"))
        control._current = str(Path(file).parent / "scene.pkg")
        return True

    control.open = open_and_resolve
    applier = _FakeApplier()
    service = ApplyService(
        conn,
        applier,
        we=control,
        dynamic_applier=WeApplier(control, sleep=lambda s: None),
        recheck_delays=(5, 30),
        sleep=lambda s: None,
        spawn=lambda target: target(),
    )

    assert service.apply(wid, project) is True

    assert applier.applied[0].name == "blackout.png"
    assert applier.restored, "the desktop picture has to come back"


def test_a_failed_live_swap_restores_the_desktop_immediately(tmp_path):
    from dailywallpaper.apply.we_applier import WeApplier

    project = tmp_path / "333" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,created_at)"
        " values('we_workshop','333','scene','x')"
    )
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    control = _FakeWe(current="D:/we/old/scene.pkg")
    control.open = lambda file, monitor=0: False
    applier = _FakeApplier()
    waits = []
    service = ApplyService(
        conn,
        applier,
        we=control,
        dynamic_applier=WeApplier(control, sleep=lambda s: None,
                                  attempts=1, interval=0.01),
        recheck_delays=(5, 30),
        sleep=waits.append,
        spawn=lambda target: target(),
    )

    assert service.apply(wid, project) is False

    assert applier.restored, "a failed swap must not leave a black desktop"
    assert not waits, "the restore is not deferred when nothing is drawing"


def test_back_to_back_swaps_keep_the_first_picture_and_drop_stale_restores(tmp_path):
    # Clicking twice in a row: the first restore must not fire into the second
    # blackout, and the second blackout must not snapshot the black picture as
    # the thing to restore -- that would leave the desktop black for good.
    from dailywallpaper.apply.we_applier import WeApplier

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,created_at)"
        " values('we_workshop','444','scene','x')"
    )
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,created_at)"
        " values('we_workshop','555','scene','x')"
    )
    conn.commit()
    ids = [row[0] for row in conn.execute("select id from wallpapers order by id")]
    wid, wid2 = ids[0], ids[1]
    control = _FakeWe(current="D:/we/old/scene.pkg")

    def open_and_resolve(file, monitor=0):
        control._current = str(Path(file).parent / "scene.pkg")
        return True

    control.open = open_and_resolve
    applier = _FakeApplier(wallpaper=str(tmp_path / "original.jpg"))
    pending = []
    service = ApplyService(
        conn,
        applier,
        we=control,
        dynamic_applier=WeApplier(control, sleep=lambda s: None),
        sleep=lambda s: None,
        spawn=pending.append,
    )

    def project(name):
        path = tmp_path / name / "project.json"
        path.parent.mkdir()
        path.write_bytes(b"x")
        return path

    assert service.apply(wid, project("a"), trigger="manual") is True
    assert service.apply(wid2, project("b"), trigger="manual") is True

    for job in pending:
        job()

    # Only the newest swap restores, and it restores the original picture --
    # not the black one the second blackout snapshotted.
    assert len(applier.restored) == 1
    assert applier.restored[0].wallpaper.endswith("original.jpg")


def test_a_pending_blackout_restore_cannot_overwrite_a_still_picture(tmp_path):
    # live change, then a still one: the blackout's delayed restore would put
    # the old desktop picture back on top of the still picture just set.
    from dailywallpaper.apply.we_applier import WeApplier

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,created_at)"
        " values('we_workshop','666','scene','x')"
    )
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,created_at)"
        " values('bing','777','image','x')"
    )
    conn.commit()
    ids = [row[0] for row in conn.execute("select id from wallpapers order by id")]
    live_id, still_id = ids[0], ids[1]
    control = _FakeWe(current="D:/we/old/scene.pkg")

    def open_and_resolve(file, monitor=0):
        control._current = str(Path(file).parent / "scene.pkg")
        return True

    control.open = open_and_resolve
    applier = _FakeApplier(wallpaper=str(tmp_path / "original.jpg"))
    pending = []
    service = ApplyService(
        conn,
        applier,
        we=control,
        dynamic_applier=WeApplier(control, sleep=lambda s: None),
        sleep=lambda s: None,
        spawn=pending.append,
    )
    live = tmp_path / "live" / "project.json"
    live.parent.mkdir()
    live.write_bytes(b"x")

    assert service.apply(live_id, live, trigger="manual") is True
    assert service.apply(still_id, tmp_path / "new.jpg", trigger="manual") is True

    for job in pending:
        job()

    # The blackout's restore must have been retired by the still change.
    assert applier.restored == []


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
    # A broken config must not block the apply; whether WE happens to be
    # drawing is a machine state, not part of this scenario.
    service = ApplyService(
        conn, _FakeApplier(), we=_BrokenWe(), we_rendering=lambda: False
    )

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


def test_the_recheck_can_read_the_epoch_from_another_thread(tmp_path):
    import threading

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    targets = []
    service = ApplyService(
        conn, _FakeApplier(), we=_ComeBackWe(),
        recheck_delays=(5, 30), sleep=lambda s: None, spawn=targets.append,
    )
    assert service.apply(wid, tmp_path / "new.jpg") is True

    worker = threading.Thread(target=targets[0])
    worker.start()
    worker.join(2)

    # Two rechecks ran (and saw a live WE current), so the image was applied
    # twice more; a thread-bound connection error would have stopped at one.
    assert len(service.applier.applied) == 3


def test_apply_refuses_an_image_below_the_screen_resolution(tmp_path):
    import numpy as np
    from PIL import Image

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    wid = _wallpaper(conn)
    applier = _FakeApplier()
    service = ApplyService(conn, applier, min_resolution=(1920, 1080))
    tiny = tmp_path / "tiny.jpg"
    bands = (np.indices((243, 432)).sum(axis=0) % 2) * 255
    Image.fromarray(np.stack([bands] * 3, axis=-1).astype(np.uint8)).save(tiny, "JPEG")

    assert service.apply(wid, tiny) is False

    assert applier.applied == []
    assert "432x243" in service.last_error
    assert conn.execute("select count(*) from change_log").fetchone()[0] == 0


def test_apply_accepts_a_full_size_image(tmp_path):
    import numpy as np
    from PIL import Image

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    wid = _wallpaper(conn)
    applier = _FakeApplier()
    service = ApplyService(conn, applier, min_resolution=(1920, 1080))
    big = tmp_path / "big.jpg"
    bands = (np.indices((1440, 2560)).sum(axis=0) % 2) * 255
    Image.fromarray(np.stack([bands] * 3, axis=-1).astype(np.uint8)).save(big, "JPEG", quality=95)

    assert service.apply(wid, big) is True

    assert len(applier.applied) == 1


def test_startup_skips_a_recorded_wallpaper_that_fails_inspection(tmp_path):
    import numpy as np
    from PIL import Image

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    tiny = tmp_path / "tiny.jpg"
    bands = (np.indices((243, 432)).sum(axis=0) % 2) * 255
    Image.fromarray(np.stack([bands] * 3, axis=-1).astype(np.uint8)).save(tiny, "JPEG")
    _logged_wallpaper(conn, tiny)
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(tiny),),
    )
    conn.commit()
    service = ApplyService(conn, _FakeApplier(), min_resolution=(1920, 1080))

    assert service.ensure_visible() is False
    assert service.applier.applied == []


def _big_image(path):
    import numpy as np
    from PIL import Image

    bands = (np.indices((1440, 2560)).sum(axis=0) % 2) * 255
    Image.fromarray(np.stack([bands] * 3, axis=-1).astype(np.uint8)).save(
        path, "JPEG", quality=95
    )
    return path


def test_keep_visible_closes_wallpaper_engine_over_our_image(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = _big_image(tmp_path / "wall.jpg")
    _logged_wallpaper(conn, image)
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(image),),
    )
    conn.commit()
    we = _FakeWe()
    applier = _FakeApplier(wallpaper=str(image.with_suffix(".apply.jpg")))
    service = ApplyService(conn, applier, we=we)

    assert service.keep_visible() is True

    assert we.closed == 1
    assert applier.applied == []


def test_keep_visible_does_nothing_when_nothing_took_over(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = _big_image(tmp_path / "wall.jpg")
    _logged_wallpaper(conn, image)
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(image),),
    )
    conn.commit()
    we = _FakeWe(current="")
    applier = _FakeApplier(wallpaper=str(image.with_suffix(".apply.jpg")))
    service = ApplyService(conn, applier, we=we)

    assert service.keep_visible() is False

    assert we.closed == 0
    assert applier.applied == []


def test_keep_visible_restores_a_desktop_something_else_changed(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = _big_image(tmp_path / "wall.jpg")
    _logged_wallpaper(conn, image)
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(image),),
    )
    conn.commit()
    applier = _FakeApplier(wallpaper="C:/somewhere/else.jpg")
    service = ApplyService(conn, applier)

    assert service.keep_visible() is True

    assert applier.applied[-1] == image


def test_a_closed_engine_is_not_closed_again_while_it_stays_closed(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = _big_image(tmp_path / "wall.jpg")
    _logged_wallpaper(conn, image)
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(image),),
    )
    conn.commit()
    we = _FakeWe()
    applier = _FakeApplier(wallpaper=str(image.with_suffix(".apply.jpg")))
    service = ApplyService(conn, applier, we=we)

    assert service.keep_visible() is True
    assert we.closed == 1

    # WE stays stopped: every check must not spawn another control command.
    we.rendering_now = False
    assert service.keep_visible() is False
    assert we.closed == 1

    # WE started drawing again.
    we.rendering_now = True
    assert service.keep_visible() is True
    assert we.closed == 2

def test_inspection_is_cached_after_preflight(tmp_path, monkeypatch):
    import dailywallpaper.analysis.quality as quality

    calls = []
    real = quality.inspect_image

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(quality, "inspect_image", counting)
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = _big_image(tmp_path / "wall.jpg")
    service = ApplyService(conn, _FakeApplier(), min_resolution=(1920, 1080))

    assert service.preflight(image) is True
    assert service.preflight(image) is True

    assert len(calls) == 1


def test_keep_visible_reopens_our_scene_when_we_shows_another(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    scene = tmp_path / "ours" / "scene.pkg"
    scene.parent.mkdir()
    scene.write_bytes(b"x")
    wid = _logged_wallpaper(conn, scene, media_type="scene")
    conn.execute("update wallpapers set file_url=? where id=?", (str(scene), wid))
    conn.commit()
    other = tmp_path / "other" / "scene.pkg"
    we = _FakeWe(current=str(other))
    service = ApplyService(conn, _FakeApplier(), we=we)

    assert service.keep_visible() is True

    assert len(we.opened) == 1
    assert Path(we.opened[0]).parent.name == "ours"


def test_keep_visible_leaves_our_playing_scene_alone(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    scene = tmp_path / "ours" / "scene.pkg"
    scene.parent.mkdir()
    scene.write_bytes(b"x")
    wid = _logged_wallpaper(conn, scene, media_type="scene")
    conn.execute("update wallpapers set file_url=? where id=?", (str(scene), wid))
    conn.commit()
    we = _FakeWe(current=str(scene))
    service = ApplyService(conn, _FakeApplier(), we=we)

    assert service.keep_visible() is False
    assert we.opened == []


def test_a_failed_reopen_is_retried_at_most_once_a_minute(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    scene = tmp_path / "ours" / "scene.pkg"
    scene.parent.mkdir()
    scene.write_bytes(b"x")
    wid = _logged_wallpaper(conn, scene, media_type="scene")
    conn.execute("update wallpapers set file_url=? where id=?", (str(scene), wid))
    conn.commit()
    other = tmp_path / "other" / "scene.pkg"
    we = _FakeWe(current=str(other))
    service = ApplyService(conn, _FakeApplier(), we=we)

    service.keep_visible()
    service.keep_visible()

    assert len(we.opened) == 1


def test_forget_banned_selection_moves_we_off_the_rated_one(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = _big_image(tmp_path / "wall.jpg")
    _logged_wallpaper(conn, image)
    image.with_suffix(".apply.jpg").write_bytes(b"x")
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(image),),
    )

    project = tmp_path / "workshop" / "999" / "project.json"
    project.parent.mkdir(parents=True)
    project.write_bytes(b"x")
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,file_url,created_at)"
        " values('we_workshop','999','scene',?,'x')",
        (str(project),),
    )
    banned = conn.execute("select max(id) from wallpapers").fetchone()[0]
    conn.execute(
        "insert into ratings(wallpaper_id,rating,created_at) values(?,1,'x')",
        (banned,),
    )
    conn.commit()

    we = _FakeWe(current=str(project.parent / "scene.pkg"))
    service = ApplyService(conn, _FakeApplier(), we=we, sleep=lambda s: None)

    assert service.forget_banned_selection() is True
    assert we.closed == 1
    assert we.replaced == [str(image.with_suffix(".apply.jpg")).replace("\\", "/")]
    # The rewrite must target the monitor this app drives, not whichever
    # entry WE happens to list first.
    assert we.replaced_monitors == [service.monitor]


def test_forget_banned_selection_leaves_a_well_rated_item_alone(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    image = _big_image(tmp_path / "wall.jpg")
    _logged_wallpaper(conn, image)
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(image),),
    )

    project = tmp_path / "workshop" / "888" / "project.json"
    project.parent.mkdir(parents=True)
    project.write_bytes(b"x")
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,file_url,created_at)"
        " values('we_workshop','888','scene',?,'x')",
        (str(project),),
    )
    good = conn.execute("select max(id) from wallpapers").fetchone()[0]
    conn.execute(
        "insert into ratings(wallpaper_id,rating,created_at) values(?,8,'x')",
        (good,),
    )
    conn.commit()

    we = _FakeWe(current=str(project.parent / "scene.pkg"))
    service = ApplyService(conn, _FakeApplier(), we=we, sleep=lambda s: None)

    assert service.forget_banned_selection() is False
    assert we.replaced == []


def test_sound_switch_is_remembered_and_sent_to_we(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    _wallpaper(conn)
    we = _FakeWe()
    service = ApplyService(conn, _FakeApplier(), we=we)

    # Silent until the switch is turned on.
    assert service.sound_enabled() is False
    assert service.set_sound(True) is True
    assert service.sound_enabled() is True
    assert we.sound_calls == ["unmute"]

    assert service.set_sound(False) is True
    assert service.sound_enabled() is False
    assert we.sound_calls == ["unmute", "mute"]


def test_a_live_wallpaper_is_muted_after_apply_when_the_switch_is_off(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,media_type,created_at)"
        " values('we_workshop','1','video','x')"
    )
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    we = _FakeWe()
    service = ApplyService(
        conn, _FakeApplier(), we=we, dynamic_applier=_FakeApplier()
    )
    service.set_sound(False)
    we.sound_calls.clear()

    assert service.apply(wid, tmp_path / "clip.mp4") is True

    assert we.sound_calls == ["mute"]


class _ModeAwareApplier(_FakeApplier):
    """Remembers the desktop mode, like the real Win32 applier."""

    class _Mode:
        def __init__(self, value):
            self.value = value
            self.set_calls = []

        def get(self):
            return self.value

        def set(self, mode):
            self.set_calls.append(mode)
            self.value = mode
            return True

    def __init__(self):
        super().__init__()
        self.background_type = self._Mode(2)  # user had a slideshow running


def test_undo_restores_the_previous_mode_and_style(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    old = tmp_path / "old.jpg"
    old.write_bytes(b"x")
    applier = _ModeAwareApplier()
    applier.current = WallpaperState(
        wallpaper=str(old), style="10", tile="1", background_type=2
    )
    service = ApplyService(conn, applier)
    wid = _wallpaper(conn)

    service.apply(wid, tmp_path / "new.jpg")
    row = conn.execute("select * from change_log").fetchone()
    assert row["previous_style"] == "10"
    assert row["previous_tile"] == "1"
    assert row["previous_background_type"] == 2

    assert service.undo_last() is True

    assert applier.restored[-1].style == "10"
    assert applier.restored[-1].tile == "1"
    assert applier.background_type.set_calls == [2]
