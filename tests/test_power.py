# tests/test_power.py
from dailywallpaper.power import PowerStatus


def test_ac_power_is_not_on_battery():
    assert PowerStatus(reader=lambda: 1).on_battery() is False


def test_battery_power_is_on_battery():
    assert PowerStatus(reader=lambda: 0).on_battery() is True


def test_unknown_status_is_treated_as_ac():
    # 255 means "unknown", e.g. on a desktop with no battery.
    assert PowerStatus(reader=lambda: 255).on_battery() is False


def test_reader_failure_is_treated_as_ac():
    def boom():
        raise OSError("no power api")

    assert PowerStatus(reader=boom).on_battery() is False
