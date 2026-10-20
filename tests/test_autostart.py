# tests/test_autostart.py
from dailywallpaper.app_autostart import VALUE_NAME, AutostartService


class FakeRegistry:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


def test_disabled_by_default():
    service = AutostartService(FakeRegistry(), command="cmd")

    assert service.enabled() is False


def test_enable_twice_leaves_one_value():
    registry = FakeRegistry()
    service = AutostartService(registry, command="cmd")

    service.enable()
    service.enable()

    assert list(registry.values) == [VALUE_NAME]
    assert registry.values[VALUE_NAME] == "cmd"


def test_sync_heals_a_lost_value():
    registry = FakeRegistry()
    service = AutostartService(registry, command="cmd")

    service.sync(True)

    assert registry.values == {VALUE_NAME: "cmd"}


def test_sync_repairs_a_stale_command():
    registry = FakeRegistry()
    registry.values[VALUE_NAME] = "old-cmd"
    service = AutostartService(registry, command="new-cmd")

    service.sync(True)

    assert registry.values == {VALUE_NAME: "new-cmd"}


def test_sync_disables_when_the_config_says_off():
    registry = FakeRegistry()
    service = AutostartService(registry, command="cmd")
    service.enable()

    service.sync(False)

    assert registry.values == {}


def test_disable_removes_the_value():
    registry = FakeRegistry()
    service = AutostartService(registry, command="cmd")
    service.enable()

    service.disable()

    assert service.enabled() is False
    assert registry.values == {}


def test_disable_when_absent_is_a_noop():
    registry = FakeRegistry()
    service = AutostartService(registry, command="cmd")

    service.disable()

    assert service.enabled() is False


def test_default_command_carries_an_absolute_config_path(tmp_path):
    from dailywallpaper.app_autostart import default_command

    command = default_command(tmp_path / "config.toml")

    assert "--config" in command
    assert str((tmp_path / "config.toml").absolute()) in command


def test_default_command_starts_the_tray_gui(tmp_path):
    from dailywallpaper.app_autostart import default_command

    command = default_command(tmp_path / "config.toml")

    assert "--headless" not in command


def test_default_command_without_a_path_falls_back_to_the_default_config(tmp_path, monkeypatch):
    from dailywallpaper import config as config_module
    from dailywallpaper.app_autostart import default_command

    monkeypatch.setattr(config_module, "default_config_path", lambda: tmp_path / "config.toml")

    command = default_command()

    assert str((tmp_path / "config.toml").absolute()) in command
