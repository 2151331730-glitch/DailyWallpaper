# dailywallpaper.spec
# Build: .venv\Scripts\pyinstaller dailywallpaper.spec --noconfirm
import sys
from pathlib import Path

project = Path(SPECPATH).resolve()
src = project / "src"
# The exe and the dist folder carry the packaged build's name, and that name is
# what identity.py hands to the Run value, the logon task, the mutex and the
# credential slot. Taking it from there keeps the four from drifting apart; the
# source tree itself keeps DailyWallpaper.
sys.path.insert(0, str(src))
from dailywallpaper.identity import PRODUCT_NAME  # noqa: E402

# Qt modules this app never imports. Each one drags DLLs and plugins along;
# cutting them here is the difference between a 400 MB folder and 250 MB.
EXCLUDES = [
    "tkinter",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.QtQuickWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtTest",
    "PySide6.QtSql",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
]

a = Analysis(
    [str(project / "packaging" / "entry.py")],
    pathex=[str(src)],
    binaries=[],
    datas=[
        (str(project / "assets" / "icon.png"), "assets"),
        (str(project / "assets" / "seed_profile.json"), "assets"),
        (str(project / "config.example.toml"), "."),
        # Shipped so a green/portable build has the same Steam guide the
        # installer drops next to the exe.
        (str(project / "docs" / "STEAM_SETUP.md"), "docs"),
        (str(project / "vendor" / "ffmpeg" / "ffmpeg.exe"), "ffmpeg"),
    ],
    hiddenimports=["keyring.backends.windows"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=PRODUCT_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project / "assets" / "icon.png"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=PRODUCT_NAME,
)
