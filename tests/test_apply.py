# tests/test_apply.py
from dailywallpaper.apply.service import ApplyService
from dailywallpaper.apply.windows_static import ApplyResult, WallpaperState
from dailywallpaper.db import connect, migrate


class _FakeApplier:
    def __init__(self, ok=True):
        self.ok = ok
        self.current = WallpaperState(wallpaper="C:/old.jpg", style="22", tile="0")
        self.restored = []

    def snapshot(self):
        return self.current

    def apply(self, image, style="fill"):
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
    return conn, _wallpaper(conn), ApplyService(conn, _FakeApplier(ok=ok), we=we)


def test_successful_apply_logs_a_static_change(tmp_path):
    conn, wid, service = _service(tmp_path)

    assert service.apply(wid, tmp_path / "new.jpg") is True

    row = conn.execute("select * from change_log").fetchone()
    assert row["success"] == 1
    assert row["method"] == "static"
    assert row["previous_wallpaper"] == "C:/old.jpg"


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

    assert service.applier.restored[-1].wallpaper == "C:/old.jpg"


def test_undo_without_history_returns_false(tmp_path):
    conn, wid, service = _service(tmp_path)

    assert service.undo_last() is False


def test_undo_reopens_the_previous_we_project(tmp_path):
    we = _FakeWe()
    conn, wid, service = _service(tmp_path, we=we)
    service.apply(wid, tmp_path / "new.jpg")

    assert service.undo_last() is True
    assert we.opened == ["D:/we/scene.pkg"]
