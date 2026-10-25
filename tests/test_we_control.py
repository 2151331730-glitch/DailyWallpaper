import json
import subprocess
from pathlib import Path
from dailywallpaper.apply.we_control import WeConfig, WeControl, same_project

FIXTURE = Path("tests/fixtures/we_config.json")
EXE = str(Path("C:/WE") / "wallpaper64.exe")

class FakeRunner:
    def __init__(self, returncode=0):
        self.calls = []
        self.returncode = returncode

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        return type("R", (), {"returncode": self.returncode})()

def test_config_finds_nested_wallpaperconfig():
    cfg = WeConfig.from_file(FIXTURE)
    assert cfg.current_file().endswith("2995369775/scene.pkg")
    assert cfg.monitor_key == "Monitor0"

def test_config_finds_flat_wallpaperconfig(tmp_path):
    flat = tmp_path / "config.json"
    flat.write_text(json.dumps({
        "win11": {
            "wallpaperconfig": {
                "selectedwallpapers": {
                    "Monitor0": {"file": "D:/flat/scene.pkg"},
                },
            },
        },
    }), encoding="utf-8")
    cfg = WeConfig.from_file(flat)
    assert cfg.current_file() == "D:/flat/scene.pkg"
    assert cfg.monitor_key == "Monitor0"

def test_open_builds_command():
    runner = FakeRunner()
    ctl = WeControl(Path("C:/WE"), FIXTURE, runner=runner)
    ctl.open(Path("D:/x/project.json"))
    assert runner.calls[0] == [
        EXE,
        "-control",
        "openWallpaper",
        "-file",
        str(Path("D:/x/project.json")),
        "-monitor",
        "0",
    ]

def test_close_builds_command():
    runner = FakeRunner()
    ctl = WeControl(Path("C:/WE"), FIXTURE, runner=runner)
    ctl.close()
    assert runner.calls[0] == [EXE, "-control", "closeWallpaper"]

def test_current_reads_config():
    ctl = WeControl(Path("C:/WE"), FIXTURE, runner=FakeRunner())
    assert ctl.current().endswith("2995369775/scene.pkg")

def test_mute_and_unmute_build_commands():
    runner = FakeRunner()
    ctl = WeControl(Path("C:/WE"), FIXTURE, runner=runner)

    ctl.mute()
    ctl.unmute()

    assert runner.calls[0] == [EXE, "-control", "mute"]
    assert runner.calls[1] == [EXE, "-control", "unmute"]


def test_replace_selection_rewrites_saved_file(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "win11": {"general": {"wallpaperconfig": {
            "selectedwallpapers": {"Monitor0": {"file": "D:/bad/scene.pkg"}},
        }}},
    }), encoding="utf-8")
    ctl = WeControl(Path("C:/WE"), cfg, runner=FakeRunner())

    assert ctl.replace_selection(Path("D:/good/1212.apply.jpg")) is True
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["win11"]["general"]["wallpaperconfig"]["selectedwallpapers"][
        "Monitor0"
    ]["file"] == str(Path("D:/good/1212.apply.jpg"))
    # Already pointing there: nothing to write.
    assert ctl.replace_selection(Path("D:/good/1212.apply.jpg")) is False


def test_same_project_accepts_we_resolved_sibling():
    # WE resolves project.json to scene.pkg (or the video file) in the same folder.
    assert same_project(
        "D:/workshop/content/431960/1132505365/scene.pkg",
        Path("D:/workshop/content/431960/1132505365/project.json"),
    )
    assert same_project(
        "D:/workshop/content/431960/1953370306/clip.mp4",
        Path("D:/workshop/content/431960/1953370306/project.json"),
    )

def test_same_project_rejects_other_folder():
    assert not same_project(
        "D:/workshop/content/431960/1111111111/scene.pkg",
        Path("D:/workshop/content/431960/2222222222/project.json"),
    )

def test_same_project_rejects_empty_current():
    assert not same_project("", Path("D:/workshop/content/431960/1132505365/project.json"))


def test_replace_selection_keeps_a_backup(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "win11": {"general": {"wallpaperconfig": {
            "selectedwallpapers": {"Monitor0": {"file": "D:/bad/scene.pkg"}},
        }}},
    }, indent="\t"), encoding="utf-8")
    ctl = WeControl(Path("C:/WE"), cfg, runner=FakeRunner())

    ctl.replace_selection(Path("D:/good/1212.apply.jpg"))

    backup = tmp_path / "config.json.bak"
    assert backup.is_file()
    assert "D:/bad/scene.pkg" in backup.read_text(encoding="utf-8")
    assert not (tmp_path / "config.json.tmp").exists()


def test_replace_selection_targets_the_monitor_we_drive(tmp_path):
    # WE keys its entries Monitor0, Monitor1, ...; the first key in the file is
    # not necessarily the screen `-monitor` addresses. Rewriting the wrong one
    # leaves WE still restoring the banned wallpaper on ours.
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "win11": {"general": {"wallpaperconfig": {
            "selectedwallpapers": {
                "Monitor0": {"file": "D:/other/desktop.jpg"},
                "Monitor1": {"file": "D:/bad/scene.pkg"},
            },
        }}},
    }), encoding="utf-8")
    ctl = WeControl(Path("C:/WE"), cfg, runner=FakeRunner())

    assert ctl.replace_selection(Path("D:/good/1212.apply.jpg"), monitor=1) is True

    saved = json.loads(cfg.read_text(encoding="utf-8"))["win11"]["general"][
        "wallpaperconfig"]["selectedwallpapers"]
    assert saved["Monitor1"]["file"] == str(Path("D:/good/1212.apply.jpg"))
    assert saved["Monitor0"]["file"] == "D:/other/desktop.jpg"


def test_replace_selection_falls_back_when_the_monitor_key_is_absent(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "win11": {"general": {"wallpaperconfig": {
            "selectedwallpapers": {"Monitor0": {"file": "D:/bad/scene.pkg"}},
        }}},
    }), encoding="utf-8")
    ctl = WeControl(Path("C:/WE"), cfg, runner=FakeRunner())

    assert ctl.replace_selection(Path("D:/good/1212.apply.jpg"), monitor=1) is True
    saved = json.loads(cfg.read_text(encoding="utf-8"))["win11"]["general"][
        "wallpaperconfig"]["selectedwallpapers"]
    assert saved["Monitor0"]["file"] == str(Path("D:/good/1212.apply.jpg"))


class TimeoutRunner:
    def __call__(self, args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 15))


def test_a_control_command_that_times_out_is_not_reported_as_a_refusal():
    # `-control` on a stopped engine boots the whole app, so the child can
    # outlive the timeout while the switch is still about to land. Treating
    # that as a refusal made the applier roll back into a second WE window.
    ctl = WeControl(Path("C:/WE"), FIXTURE, runner=TimeoutRunner())

    assert ctl.open(Path("D:/x/project.json")) is True


def test_running_asks_the_injected_probe():
    assert WeControl(Path("C:/WE"), FIXTURE, runner=FakeRunner(),
                     processes=lambda: True).running() is True
    assert WeControl(Path("C:/WE"), FIXTURE, runner=FakeRunner(),
                     processes=lambda: False).running() is False


def test_running_defaults_to_false_when_the_probe_explodes():
    def boom():
        raise OSError("tasklist missing")

    assert WeControl(Path("C:/WE"), FIXTURE, runner=FakeRunner(),
                     processes=boom).running() is False
