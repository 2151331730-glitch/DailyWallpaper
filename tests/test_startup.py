# tests/test_startup.py
"""The boot-time pass that puts the parked wallpaper up before the GUI."""
import json
from datetime import datetime
from types import SimpleNamespace

from dailywallpaper.app_autostart import (
    ensure_logon_task,
    logon_task_action,
    logon_task_exists,
    split_command,
)
from dailywallpaper.db import connect, migrate
from dailywallpaper.startup import apply_parked
from dailywallpaper.state.machine import PREPARED_KEY


def _cfg(data_dir):
    return SimpleNamespace(
        app=SimpleNamespace(
            data_dir=str(data_dir),
            daily_time="08:30",
            latest_change_time="22:00",
            manual_change_counts_toward_daily_limit=True,
        ),
        wallpaper=SimpleNamespace(dynamic_requires_ac=False),
    )


class _FakeApply:
    def __init__(self, conn):
        self.conn = conn
        self.calls = []

    def apply(self, wallpaper_id, image_path, trigger="auto"):
        self.calls.append((wallpaper_id, str(image_path), trigger))
        return True


def _park(conn, image, wallpaper_id=42):
    conn.execute(
        "insert or replace into state(key,value) values(?,?)",
        (
            PREPARED_KEY,
            json.dumps(
                {
                    "wallpaper_id": wallpaper_id,
                    "apply_path": str(image),
                    "image_path": str(image),
                    "decision_id": None,
                    "media_kind": "image",
                    "reason": "parked",
                }
            ),
        ),
    )
    conn.commit()


def _patch_apply_service(monkeypatch, fake):
    monkeypatch.setattr(
        "dailywallpaper.apply.factory.build_apply_service",
        lambda cfg, conn: fake,
    )


def test_applies_the_parked_wallpaper_before_anything_else(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    image = data / "42.apply.jpg"
    image.write_bytes(b"x")
    conn = connect(data / "app.db")
    migrate(conn)
    _park(conn, image)
    fake = _FakeApply(conn)
    _patch_apply_service(monkeypatch, fake)
    conn.close()

    assert apply_parked(_cfg(data)) is True

    assert fake.calls == [(42, str(image), "auto")]
    after = connect(data / "app.db")
    assert (
        after.execute("select 1 from state where key=?", (PREPARED_KEY,)).fetchone()
        is None
    )
    after.close()


def test_nothing_parked_means_no_apply(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    conn = connect(data / "app.db")
    migrate(conn)
    fake = _FakeApply(conn)
    _patch_apply_service(monkeypatch, fake)
    conn.close()

    assert apply_parked(_cfg(data)) is False
    assert fake.calls == []


def test_a_parked_file_that_is_gone_is_not_applied(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    conn = connect(data / "app.db")
    migrate(conn)
    _park(conn, data / "gone.apply.jpg")
    fake = _FakeApply(conn)
    _patch_apply_service(monkeypatch, fake)
    conn.close()

    assert apply_parked(_cfg(data)) is False
    assert fake.calls == []


def test_a_broken_database_does_not_take_the_launch_down(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()

    def boom(cfg, conn):
        raise RuntimeError("no WE")

    monkeypatch.setattr("dailywallpaper.apply.factory.build_apply_service", boom)

    assert apply_parked(_cfg(data)) is False


def test_split_command_separates_a_quoted_exe():
    exe, args = split_command(
        '"C:\\x\\pythonw.exe" -m dailywallpaper --config "C:\\y\\config.toml"'
    )

    assert exe == "C:\\x\\pythonw.exe"
    assert args == '-m dailywallpaper --config "C:\\y\\config.toml"'


_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{args}</Arguments>
    </Exec>
  </Actions>
</Task>"""


class _XmlResult:
    returncode = 0

    def __init__(self, text):
        self.stdout = text.encode("utf-16")


def test_logon_task_action_reads_the_registered_command(monkeypatch):
    monkeypatch.setattr(
        "dailywallpaper.app_autostart._schtasks",
        lambda *a, **k: _XmlResult(
            _TASK_XML.format(exe=r"C:\x\pythonw.exe", args="-m dailywallpaper")
        ),
    )

    assert logon_task_action("DailyWallpaperTest") == (
        r"C:\x\pythonw.exe",
        "-m dailywallpaper",
    )


def test_a_moved_venv_re_registers_the_logon_task(monkeypatch):
    """A stale task would start the old copy, which wins the instance mutex."""
    scripts = []
    monkeypatch.setattr(
        "dailywallpaper.app_autostart.logon_task_action",
        lambda name="x": (r"C:\old\pythonw.exe", "-m dailywallpaper"),
    )
    monkeypatch.setattr(
        "dailywallpaper.app_autostart._powershell",
        lambda script: scripts.append(script) or SimpleNamespace(returncode=0),
    )

    ok = ensure_logon_task(
        '"C:\\new\\pythonw.exe" -m dailywallpaper', "DailyWallpaperTest"
    )

    assert ok is True
    assert len(scripts) == 1
    assert "C:\\new\\pythonw.exe" in scripts[0]


def test_a_current_logon_task_is_left_alone(monkeypatch):
    scripts = []
    monkeypatch.setattr(
        "dailywallpaper.app_autostart.logon_task_action",
        lambda name="x": (r"C:\new\pythonw.exe", "-m dailywallpaper"),
    )
    monkeypatch.setattr(
        "dailywallpaper.app_autostart._powershell",
        lambda script: scripts.append(script) or SimpleNamespace(returncode=0),
    )

    assert ensure_logon_task(
        '"C:\\new\\pythonw.exe" -m dailywallpaper', "DailyWallpaperTest"
    )
    assert scripts == []


def test_logon_task_is_a_no_op_when_the_tooling_is_missing(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("not available")

    monkeypatch.setattr("dailywallpaper.app_autostart._schtasks", unavailable)
    monkeypatch.setattr("dailywallpaper.app_autostart._powershell", unavailable)

    assert logon_task_exists("DailyWallpaperTest") is False
    assert (
        ensure_logon_task('"pythonw.exe" -m dailywallpaper', "DailyWallpaperTest")
        is False
    )


def test_rehydrate_only_serves_a_day_that_is_already_prepared(tmp_path):
    """Rebuild the shortlist after a restart, never after a failed prepare."""
    from dailywallpaper.app import RealPipeline
    from dailywallpaper.db import connect, migrate

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    pipeline = RealPipeline.__new__(RealPipeline)
    pipeline.conn = conn
    pipeline.shortlist = []
    forced = []

    def prepare(now, force=False, dry_run=False):
        forced.append(force)
        pipeline.shortlist = ["candidate"]

    pipeline.prepare = prepare
    now = datetime(2026, 10, 24, 12, 0)

    # The day was never prepared: a failed prepare must keep its backoff.
    assert pipeline.rehydrate(now) is False
    assert forced == []

    conn.execute(
        "insert into state(key,value) values('prepared_2026-10-24','1')"
    )
    conn.commit()

    assert pipeline.rehydrate(now) is True
    assert forced == [True]


def test_rehydrate_leaves_a_working_shortlist_alone(tmp_path):
    from dailywallpaper.app import RealPipeline
    from dailywallpaper.db import connect, migrate

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into state(key,value) values('prepared_2026-10-24','1')")
    conn.commit()
    pipeline = RealPipeline.__new__(RealPipeline)
    pipeline.conn = conn
    pipeline.shortlist = ["already here"]
    pipeline.prepare = lambda *a, **k: (_ for _ in ()).throw(AssertionError("refetched"))

    assert pipeline.rehydrate(datetime(2026, 10, 24, 12, 0)) is False
