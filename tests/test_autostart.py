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
