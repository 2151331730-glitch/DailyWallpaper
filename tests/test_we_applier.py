# tests/test_we_applier.py
from pathlib import Path

from dailywallpaper.apply.we_applier import WeApplier


class FakeControl:
    def __init__(self, ok=True, resolves=True):
        self.ok = ok
        self.resolves = resolves
        self.current_value = "D:/we/old/scene.pkg"
        self.opened = []
        self.replaced = []
        self.order = []
        self.closed = 0

    def current(self):
        return self.current_value

    def close(self):
        self.closed += 1
        self.order.append("close")
        return True

    def replace_selection(self, file, monitor=0):
        self.replaced.append(Path(file))
        self.order.append("replace")
        return True

    def open(self, file, monitor=0):
        self.opened.append(Path(file))
        self.order.append("open")
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


class ColdStartControl:
    """WE is stopped: ``-control`` boots it and the switch only lands later.

    ``running`` answers False for the first ``boot_calls`` probes, which is
    what a cold engine looks like while Steam init and CEF spin up.
    """

    def __init__(self, boot_calls=4, resolves=True):
        self.boot_calls = boot_calls
        self.resolves = resolves
        self.running_calls = 0
        self.start_calls = 0
        self.closed = 0
        self.current_value = "D:/we/old/scene.pkg"
        self.opened = []

    def start(self):
        self.start_calls += 1
        return True

    def close(self):
        self.closed += 1
        return True

    def running(self):
        self.running_calls += 1
        return self.running_calls > self.boot_calls

    def current(self):
        return self.current_value

    def open(self, file, monitor=0):
        self.opened.append(Path(file))
        if self.resolves:
            self.current_value = str(Path(file).parent / "scene.pkg")
        return True


def _project(tmp_path):
    project = tmp_path / "new" / "project.json"
    project.parent.mkdir()
    project.write_bytes(b"x")
    return project


def test_a_cold_engine_is_given_time_to_boot_before_the_switch_is_judged(tmp_path):
    # Five seconds was the whole budget: a WE that boots in ~25 s always lost,
    # so every "换一张" onto a live wallpaper failed on a stopped engine.
    project = _project(tmp_path)
    control = ColdStartControl(boot_calls=4, resolves=True)

    result = _applier(control).apply(project)

    assert result.ok is True
    assert control.opened == [project]
    assert control.running_calls > 4
    # The engine is booted detached first: `-control` would have started it as
    # a child the command's own timeout then kills.
    assert control.start_calls == 1


def test_a_cold_start_that_never_boots_fails_without_reopening_the_old_project(tmp_path):
    # The rollback opened the previous project again, which launched a second
    # WE window. A cold failure must leave the desktop alone.
    project = _project(tmp_path)
    control = ColdStartControl(boot_calls=10_000, resolves=False)

    result = _applier(control).apply(project)

    assert result.ok is False
    assert "start" in result.error
    # Nothing was opened at all, so there is nothing to roll back into a
    # second window either.
    assert control.opened == []


def test_a_cold_engine_that_boots_but_never_switches_does_not_roll_back(tmp_path):
    project = _project(tmp_path)
    control = ColdStartControl(boot_calls=2, resolves=False)

    result = _applier(control).apply(project)

    assert result.ok is False
    assert "did not switch" in result.error
    assert control.opened == [project]


def test_an_engine_that_was_already_running_still_rolls_back(tmp_path):
    # Here the rollback costs nothing: WE is up, so reopening the previous
    # project cannot pop a second window.
    project = _project(tmp_path)
    control = FakeControl(resolves=False)
    control.running = lambda: True

    result = _applier(control).apply(project)

    assert result.ok is False
    assert control.opened[-1] == Path("D:/we/old/scene.pkg")


def test_the_saved_selection_is_aimed_at_the_target_before_the_switch(tmp_path):
    # WE renders whatever config.json names until the new project finishes
    # loading, which looked like the previous wallpaper flashing back.
    project = _project(tmp_path)
    control = FakeControl()

    result = _applier(control).apply(project)

    assert result.ok is True
    assert control.replaced == [project]


def test_a_scene_project_is_recorded_as_its_packed_file(tmp_path):
    # WE's own config names scene.pkg, never project.json; writing the manifest
    # path there would hand it a record it cannot resolve.
    folder = tmp_path / "scene"
    folder.mkdir()
    project = folder / "project.json"
    project.write_text('{"type": "Scene", "file": "scene.json"}', encoding="utf-8")
    (folder / "scene.pkg").write_bytes(b"pkg")
    control = FakeControl()

    _applier(control).apply(project)

    assert control.replaced == [folder / "scene.pkg"]


def test_a_web_project_is_recorded_as_its_declared_entry(tmp_path):
    folder = tmp_path / "web"
    folder.mkdir()
    project = folder / "project.json"
    project.write_text('{"type": "Web", "file": "index.html"}', encoding="utf-8")
    (folder / "index.html").write_text("<html></html>", encoding="utf-8")
    control = FakeControl()

    _applier(control).apply(project)

    assert control.replaced == [folder / "index.html"]


def test_switching_never_stops_the_current_wallpaper_first(tmp_path):
    # Stopping it left a static gap between the two live wallpapers; the old
    # one has to stay on screen until WE has the new one ready.
    project = _project(tmp_path)
    control = FakeControl()

    _applier(control).apply(project)

    assert control.order == ["replace", "open"]
    assert control.closed == 0
