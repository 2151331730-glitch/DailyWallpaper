import os
from pathlib import Path
from PIL import Image
import pytest
from dailywallpaper.apply.windows_static import (
    WallpaperState, WindowsStaticApplier, convert_to_jpeg,
)

class FakeRegistry:
    def __init__(self):
        self.values = {"WallPaper": "C:/old.jpg", "WallpaperStyle": "10", "TileWallpaper": "0"}
    def get(self, name): return self.values[name]
    def set(self, name, value): self.values[name] = value

class FakeSpi:
    def __init__(self, fail=False):
        self.path = "C:/old.jpg"
        self.fail = fail
        self.calls = []
    def set_wallpaper(self, path):
        self.calls.append(("set", str(path)))
        if self.fail: return False
        self.path = str(path)
        return True
    def get_wallpaper(self):
        return self.path

class AbsolutePathSpi(FakeSpi):
    """Mimics SPI_GETDESKWALLPAPER returning an absolute, uppercase path."""
    def set_wallpaper(self, path):
        ok = super().set_wallpaper(path)
        if ok:
            self.path = os.path.abspath(str(path)).upper()
        return ok

def test_convert_to_jpeg(tmp_path):
    src = tmp_path / "a.png"
    dst = tmp_path / "a.jpg"
    Image.new("RGB", (2560, 1440), (20, 40, 60)).save(src)
    out = convert_to_jpeg(src, dst)
    assert out.exists()
    assert Image.open(out).size == (2560, 1440)

def test_apply_saves_and_verifies(tmp_path):
    img = tmp_path / "wall.jpg"
    Image.new("RGB", (2560, 1440), (10, 20, 30)).save(img)
    reg, spi = FakeRegistry(), FakeSpi()
    applier = WindowsStaticApplier(spi, reg)
    state = applier.snapshot()
    result = applier.apply(img)
    assert result.ok
    assert spi.path == str(img.with_suffix(".apply.jpg"))
    assert state.wallpaper == "C:/old.jpg"

def test_apply_failure_restores(tmp_path):
    img = tmp_path / "wall.jpg"
    Image.new("RGB", (2560, 1440), (10, 20, 30)).save(img)
    reg, spi = FakeRegistry(), FakeSpi(fail=True)
    applier = WindowsStaticApplier(spi, reg)
    result = applier.apply(img)
    assert not result.ok
    assert spi.path == "C:/old.jpg"
    # restore() must have re-applied the original wallpaper after the failure.
    assert spi.calls[-1] == ("set", str(Path("C:/old.jpg")))

def test_apply_accepts_absolute_case_different_spi_path(tmp_path, monkeypatch):
    # The probe passes a relative path, while SPI_GETDESKWALLPAPER returns an
    # absolute path (often with different case). apply() must still verify OK.
    monkeypatch.chdir(tmp_path)
    img = Path("wall.jpg")
    Image.new("RGB", (2560, 1440), (10, 20, 30)).save(img)
    reg, spi = FakeRegistry(), AbsolutePathSpi()
    applier = WindowsStaticApplier(spi, reg)
    result = applier.apply(img)
    assert result.ok
    assert spi.path == os.path.abspath(str(img.with_suffix(".apply.jpg"))).upper()
