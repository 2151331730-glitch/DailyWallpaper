# tests/test_cli.py
import logging
import uuid

import pytest

from dailywallpaper.__main__ import main
from dailywallpaper.app import Application, SingleInstance
from dailywallpaper.logging_setup import redact, setup_logging


def _config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'[app]\ndata_dir = "{(tmp_path / "data").as_posix()}"\nlog_level = "INFO"\n',
        encoding="utf-8",
    )
    return path


def test_redact_removes_key_shapes():
    assert "sk-abc123" not in redact("key sk-abc123")
    assert "xyz" not in redact("Authorization: Bearer xyz")
    assert "api_key=secret" not in redact("api_key=secret")


def test_redacts_secrets_written_to_the_log(tmp_path):
    setup_logging(tmp_path, "INFO", 5, 5)
    logging.getLogger("test").info("key=sk-abc123 Bearer xyz")
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")

    assert "sk-abc123" not in text
    assert "Bearer xyz" not in text


class _App:
    def __init__(self, calls):
        self.calls = calls

    def run_once(self, dry_run=False):
        self.calls.append(("once", dry_run))
        return 0

    def run_forever(self):
        self.calls.append(("forever", None))
        return 0


def test_cli_once_runs_a_single_tick(tmp_path):
    calls = []
    cfg = _config(tmp_path)

    code = main(["--config", str(cfg), "--once"], app_factory=lambda c: _App(calls))

    assert code == 0
    assert calls == [("once", False)]


def test_cli_dry_run_is_forwarded(tmp_path):
    calls = []
    cfg = _config(tmp_path)

    main(["--config", str(cfg), "--once", "--dry-run"], app_factory=lambda c: _App(calls))

    assert calls == [("once", True)]


def test_cli_headless_runs_forever(tmp_path):
    calls = []
    cfg = _config(tmp_path)

    main(["--config", str(cfg), "--headless"], app_factory=lambda c: _App(calls))

    assert calls == [("forever", None)]


def test_application_run_once_delegates_to_the_machine():
    from dailywallpaper.state.machine import TickResult

    class _Machine:
        def __init__(self):
            self.calls = []

        def tick(self, dry_run=False):
            self.calls.append(dry_run)
            return TickResult("dry_run" if dry_run else "applied")

    machine = _Machine()
    app = Application(machine, sleep=lambda seconds: None)

    assert app.run_once(dry_run=True) == 0
    assert machine.calls == [True]


def test_run_forever_survives_a_failing_tick():
    class _Machine:
        def __init__(self):
            self.calls = 0

        def tick(self, dry_run=False):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")
            return 0

    machine = _Machine()
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise SystemExit

    app = Application(machine, sleep=sleep)

    with pytest.raises(SystemExit):
        app.run_forever()

    assert machine.calls == 2


def test_single_instance_rejects_a_second_owner():
    name = "DailyWallpaperTest_" + uuid.uuid4().hex[:8]

    with SingleInstance(name):
        with pytest.raises(RuntimeError):
            with SingleInstance(name):
                pass


def test_cli_headless_flag_skips_the_gui(tmp_path):
    calls = []
    cfg = _config(tmp_path)

    main(["--config", str(cfg), "--once", "--headless"], app_factory=lambda c: _App(calls))

    assert calls == [("once", False)]


def test_cli_runs_the_gui_when_not_headless(tmp_path):
    calls = []
    cfg = _config(tmp_path)

    code = main(["--config", str(cfg)], app_factory=lambda c: _App(calls), run_gui=lambda cfg: 0)

    assert code == 0
    assert calls == []
