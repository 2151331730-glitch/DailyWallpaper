# tests/test_desktop_theme.py
from dailywallpaper.apply.desktop_theme import (
    PERSONALIZE_KEY,
    SEARCH_KEY,
    SEARCH_ICON_ONLY,
    darken_taskbar,
)


class _Registry:
    def __init__(self, deny=()):
        self.values = {}
        self.deny = set(deny)

    def set_dword(self, sub_key, name, value):
        if sub_key in self.deny:
            raise PermissionError("[WinError 5] Access is denied")
        self.values[(sub_key, name)] = value


def test_darken_taskbar_sets_both_values():
    registry = _Registry()
    broadcasts = []

    result = darken_taskbar(registry=registry, broadcast=lambda: broadcasts.append(1))

    assert registry.values[(PERSONALIZE_KEY, "SystemUsesLightTheme")] == 0
    assert registry.values[(SEARCH_KEY, "SearchboxTaskbarMode")] == SEARCH_ICON_ONLY
    assert result == {"taskbar": "applied", "search": "applied"}
    assert broadcasts == [1]


def test_a_locked_search_key_is_reported_not_raised():
    registry = _Registry(deny={SEARCH_KEY})

    result = darken_taskbar(registry=registry, broadcast=lambda: None)

    assert result["taskbar"] == "applied"
    assert "denied" in result["search"].lower()


def test_a_locked_theme_key_is_reported_not_raised():
    registry = _Registry(deny={PERSONALIZE_KEY})

    result = darken_taskbar(registry=registry, broadcast=lambda: None)

    assert "denied" in result["taskbar"].lower()
    assert result["search"] == "applied"
