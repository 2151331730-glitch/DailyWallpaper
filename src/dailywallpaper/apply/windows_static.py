from __future__ import annotations

import ctypes
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image

SPI_SETDESKWALLPAPER = 20
SPI_GETDESKWALLPAPER = 0x0073
SPIF_UPDATEINIFILE = 0x01
SPIF_SENDCHANGE = 0x02
STYLE_MAP = {"center": "0", "stretch": "2", "fit": "6", "fill": "10"}

class RegistryLike(Protocol):
    def get(self, name: str) -> str: ...
    def set(self, name: str, value: str) -> None: ...

class SpiLike(Protocol):
    def set_wallpaper(self, path: Path) -> bool: ...
    def get_wallpaper(self) -> str: ...

class Win32Registry:
    KEY = r"Control Panel\Desktop"
    def get(self, name: str) -> str:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY) as k:
            return str(winreg.QueryValueEx(k, name)[0])
    def set(self, name: str, value: str) -> None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

class Win32Spi:
    def set_wallpaper(self, path: Path) -> bool:
        return bool(ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETDESKWALLPAPER, 0, str(path),
            SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
        ))
    def get_wallpaper(self) -> str:
        buf = ctypes.create_unicode_buffer(260)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETDESKWALLPAPER, len(buf), buf, 0,
        )
        return buf.value if ok else ""

@dataclass(frozen=True)
class WallpaperState:
    wallpaper: str
    style: str
    tile: str

@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    error: str = ""

def convert_to_jpeg(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im.convert("RGB").save(dest, "JPEG", quality=95)
    return dest

class WindowsStaticApplier:
    def __init__(self, spi: SpiLike | None = None, registry: RegistryLike | None = None):
        self.spi = spi or Win32Spi()
        self.registry = registry or Win32Registry()

    def snapshot(self) -> WallpaperState:
        return WallpaperState(
            wallpaper=self.registry.get("WallPaper"),
            style=self.registry.get("WallpaperStyle"),
            tile=self.registry.get("TileWallpaper"),
        )

    def apply(self, image: Path, style: str = "fill") -> ApplyResult:
        state = self.snapshot()
        try:
            jpeg = convert_to_jpeg(image, image.with_suffix(".apply.jpg"))
            self.registry.set("WallpaperStyle", STYLE_MAP[style])
            self.registry.set("TileWallpaper", "0")
            if not self.spi.set_wallpaper(jpeg):
                raise OSError("SystemParametersInfoW returned 0")
            # Verify via SPI_GETDESKWALLPAPER; registry is only an auxiliary check.
            if self.spi.get_wallpaper().lower() != str(jpeg).lower():
                raise OSError("wallpaper verification failed")
            return ApplyResult(ok=True)
        except Exception as exc:
            self.restore(state)
            return ApplyResult(ok=False, error=str(exc))

    def restore(self, state: WallpaperState) -> None:
        self.registry.set("WallpaperStyle", state.style)
        self.registry.set("TileWallpaper", state.tile)
        if state.wallpaper:
            self.spi.set_wallpaper(Path(state.wallpaper))
