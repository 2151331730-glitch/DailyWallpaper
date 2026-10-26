# tests/test_identity.py
"""Two builds, two names -- Windows deletes these strings by name.

The uninstaller removes the Run value, the logon task (through ``--forget``)
and the credential-manager service. While the packaged build answered to the
source tree's name, installing it and then uninstalling it took the developer
build's autostart and API keys with it (2026-10-25).
"""
import sys
from pathlib import Path

from dailywallpaper import identity
from dailywallpaper.analysis import vision
from dailywallpaper.config import user_data_dir

ROOT = Path(__file__).resolve().parents[1]


def test_source_tree_keeps_its_historical_name():
    assert identity.packaged() is False
    assert identity.name() == "DailyWallpaper"
    assert identity.title() == "每日壁纸"


def test_packaged_build_answers_to_the_product_name(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert identity.packaged() is True
    assert identity.name() == identity.PRODUCT_NAME
    assert identity.title() == identity.PRODUCT_TITLE
    assert identity.PRODUCT_NAME != identity.SOURCE_NAME


def test_product_name_is_usable_as_a_windows_name():
    """It becomes a folder, an exe and a registry value, and is read aloud."""
    name = identity.PRODUCT_NAME
    assert name.isascii()
    assert " " not in name
    assert name.isalnum()


def test_consumers_take_the_name_from_identity():
    from dailywallpaper import app_autostart
    from dailywallpaper.__main__ import INSTANCE_NAME

    assert vision.KEYRING_SERVICE == identity.name()
    assert app_autostart.VALUE_NAME == identity.name()
    assert app_autostart.TASK_NAME == identity.name()
    assert INSTANCE_NAME == identity.name()


def test_installed_data_dir_is_the_product_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("DAILYWALLPAPER_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert user_data_dir() == tmp_path / identity.PRODUCT_NAME


def test_installer_deletes_only_its_own_name():
    """The uninstaller's Run value and data folder must match the build.

    Compiled separately from the Python package, so nothing but this test
    notices when the two drift apart -- and drifting is exactly how the
    source build lost its autostart.
    """
    iss = (ROOT / "installer.iss").read_text(encoding="utf-8")

    assert f'#define MyAppExeName "{identity.PRODUCT_NAME}.exe"' in iss
    assert f"dist\\{identity.PRODUCT_NAME}\\" in iss
    assert f"{{localappdata}}\\Programs\\{identity.PRODUCT_NAME}" in iss
    # The Run value removed on uninstall, and the data folder it offers to wipe.
    assert f"'{identity.PRODUCT_NAME}');" in iss
    assert f"{{localappdata}}\\{identity.PRODUCT_NAME}'" in iss
    # A leftover reference in code (not a comment) would delete or inspect the
    # source build's keys instead of this build's.
    code = [
        line for line in iss.splitlines() if not line.lstrip().startswith(";")
    ]
    assert "DailyWallpaper" not in "\n".join(code)


def test_spec_names_the_frozen_build_from_identity():
    spec = (ROOT / "dailywallpaper.spec").read_text(encoding="utf-8")

    assert "from dailywallpaper.identity import PRODUCT_NAME" in spec
    assert spec.count("name=PRODUCT_NAME") == 2
    assert 'name="DailyWallpaper"' not in spec
