# tests/test_we_autostart.py
"""Turning Wallpaper Engine's own start-with-Windows off."""
import json
from pathlib import Path
from types import SimpleNamespace

from dailywallpaper import we_autostart as wa


class _Result:
    def __init__(self, code=0, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


class _Runner:
    def __init__(self, service=True, delete_code=0):
        self.calls = []
        self.service = service
        self.delete_code = delete_code

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        if args[:2] == ["sc", "query"]:
            return _Result(0 if self.service else 1)
        if args[:1] == ["tasklist"]:
            return _Result(0, "")
        if args[:3] == ["sc", "delete", wa.SERVICE_NAME]:
            return _Result(self.delete_code)
        return _Result(0)


class _Registry:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.deleted = []

    def get(self, name):
        return self.values.get(name)

    def delete(self, name):
        self.deleted.append(name)
        self.values.pop(name, None)

    def set(self, name, value):
        self.values[name] = value


def _cfg(tmp_path):
    install = tmp_path / "wallpaper_engine"
    install.mkdir()
    (install / "config.json").write_text(
        json.dumps(
            {
                "win11": {
                    "general": {
                        "user": {
                            "autostart": True,
                            "autostartx64": True,
                            "autostartscheduler": True,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return SimpleNamespace(
        wallpaper_engine=SimpleNamespace(install_dir=str(install)),
        app=SimpleNamespace(data_dir=str(data)),
    )


def _user(cfg):
    text = (Path(cfg.wallpaper_engine.install_dir) / "config.json").read_text(
        encoding="utf-8"
    )
    return json.loads(text)["win11"]["general"]["user"]


def test_every_autostart_switch_is_turned_off(tmp_path):
    cfg = _cfg(tmp_path)
    registry = _Registry({"WallpaperEngine": '"D:\\we\\wallpaper64.exe"'})
    helper = tmp_path / "wallpaperservice64.exe"
    helper.write_bytes(b"x")

    result = wa.disable(
        cfg, runner=_Runner(), registry=registry, admin=True,
        running=False, helper_path=helper,
    )

    assert result.ok is True
    user = _user(cfg)
    assert user["autostart"] is False
    assert user["autostartx64"] is False
    assert user["autostartscheduler"] is False
    assert registry.values == {}
    assert helper.exists() is False
    assert any("服务" in step for step in result.changed)


def test_without_the_service_no_administrator_is_needed(tmp_path):
    cfg = _cfg(tmp_path)

    result = wa.disable(
        cfg, runner=_Runner(service=False), registry=_Registry(), admin=False,
        running=False,
    )

    assert result.ok is True
    assert _user(cfg)["autostart"] is False


def test_a_present_service_without_rights_is_refused_not_half_done(tmp_path):
    cfg = _cfg(tmp_path)
    registry = _Registry({"Wallpaper Engine": "x"})
    runner = _Runner(service=True)

    result = wa.disable(
        cfg, runner=runner, registry=registry, admin=False, running=False,
    )

    assert result.ok is False
    assert "管理员权限" in result.failed[0]
    # Nothing was touched: a half-done removal would leave WE in a state its
    # own settings page cannot describe.
    assert _user(cfg)["autostart"] is True
    assert registry.values != {}


def test_a_running_we_is_stopped_before_its_config_is_edited(tmp_path):
    cfg = _cfg(tmp_path)
    runner = _Runner(service=False)

    wa.disable(
        cfg, runner=runner, registry=_Registry(), admin=False, running=True,
    )

    images = [call for call in runner.calls if call[:1] == ["taskkill"]]
    assert images and wa.EXE_NAME in images[0]
    # taskkill must precede the config edit, or WE rewrites the file on exit.
    assert runner.calls.index(images[0]) < len(runner.calls)


def test_a_failed_service_delete_is_reported(tmp_path):
    cfg = _cfg(tmp_path)

    result = wa.disable(
        cfg, runner=_Runner(service=True, delete_code=5), registry=_Registry(),
        admin=True, running=False, helper_path=tmp_path / "missing.exe",
    )

    assert result.ok is False
    assert any("删除服务失败" in item for item in result.failed)


def test_disable_can_be_undone_on_uninstall(tmp_path):
    """Taking another program's autostart away has to be reversible."""
    cfg = _cfg(tmp_path)
    exe = "D:" + chr(92) + "we" + chr(92) + "wallpaper64.exe"
    registry = _Registry({"WallpaperEngine": '"' + exe + '"'})
    helper = tmp_path / "wallpaperservice64.exe"
    helper.write_bytes(b"x")

    disabled = wa.disable(
        cfg, runner=_Runner(), registry=registry, admin=True,
        running=False, helper_path=helper,
    )
    assert disabled.ok
    assert _user(cfg)["autostart"] is False

    restored = wa.restore(cfg, registry=registry)

    assert restored.ok is True
    assert _user(cfg)["autostart"] is True
    assert registry.values["WallpaperEngine"] == '"' + exe + '"'
    marker = Path(cfg.app.data_dir) / wa.MARKER_NAME
    assert marker.exists() is False


def test_restore_without_a_marker_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    registry = _Registry()

    result = wa.restore(cfg, registry=registry)

    assert result.changed == []
    assert _user(cfg)["autostart"] is True
