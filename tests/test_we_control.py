from pathlib import Path
from dailywallpaper.apply.we_control import WeConfig, WeControl

FIXTURE = Path("tests/fixtures/we_config.json")

def test_config_finds_top_level_object():
    cfg = WeConfig.from_file(FIXTURE)
    assert cfg.current_file().endswith("2995369775/scene.pkg")
    assert cfg.monitor_key == "Monitor0"

def test_open_builds_command(tmp_path):
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return type("R", (), {"returncode": 0})()
    ctl = WeControl(Path("C:/WE"), FIXTURE, runner=runner)
    ctl.open(Path("D:/x/project.json"))
    assert calls[0][0].endswith("wallpaper64.exe")
    assert "-control" in calls[0]
    assert "openWallpaper" in calls[0]
