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
