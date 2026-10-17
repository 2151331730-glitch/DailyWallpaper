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

def test_config_finds_top_level_object():
    cfg = WeConfig.from_file(FIXTURE)
    assert cfg.current_file().endswith("2995369775/scene.pkg")
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
