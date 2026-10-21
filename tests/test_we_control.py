import json
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
