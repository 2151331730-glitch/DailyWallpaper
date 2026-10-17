import json
from pathlib import Path
from dailywallpaper.apply.we_control import WeConfig, WeControl

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
