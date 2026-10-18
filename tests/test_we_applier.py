# tests/test_we_applier.py
from pathlib import Path

from dailywallpaper.apply.we_applier import WeApplier


class FakeControl:
    def __init__(self, ok=True, resolves=True):
        self.ok = ok
        self.resolves = resolves
        self.current_value = "D:/we/old/scene.pkg"
        self.opened = []

    def current(self):
        return self.current_value

    def open(self, file, monitor=0):
        self.opened.append(Path(file))
        if self.ok and self.resolves:
            self.current_value = str(Path(file).parent / "scene.pkg")
        return self.ok


def _applier(control):
    return WeApplier(control, sleep=lambda seconds: None)


def test_apply_switches_and_verifies(tmp_path):
    project = tmp_path / "new" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")
    control = FakeControl()

    result = _applier(control).apply(project)

    assert result.ok is True
    assert control.opened[0] == project


def test_snapshot_records_the_current_file(tmp_path):
    control = FakeControl()

    assert _applier(control).snapshot().file == "D:/we/old/scene.pkg"


def test_refused_switch_restores_the_previous_project(tmp_path):
    project = tmp_path / "new" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")
    control = FakeControl(ok=False)

    result = _applier(control).apply(project)

    assert result.ok is False
    assert control.opened[-1] == Path("D:/we/old/scene.pkg")


def test_unverified_switch_times_out_and_restores(tmp_path):
    project = tmp_path / "new" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")
    control = FakeControl(resolves=False)

    result = _applier(control).apply(project)

    assert result.ok is False
    assert "did not switch" in result.error
    assert control.opened[-1] == Path("D:/we/old/scene.pkg")


def test_restore_opens_the_saved_file(tmp_path):
    control = FakeControl()
    applier = _applier(control)
    state = applier.snapshot()

    applier.restore(state)

    assert control.opened[-1] == Path("D:/we/old/scene.pkg")
