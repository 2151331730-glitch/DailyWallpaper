# tests/test_config.py
from pathlib import Path
import pytest
from dailywallpaper.config import load_config, default_config_path


def test_load_example_config():
    cfg = load_config(Path("config.example.toml"))
    assert cfg.app.daily_time == "08:30"
    assert cfg.app.latest_change_time == "22:00"
    assert cfg.budget.monthly_cny == 10.0
    assert cfg.vision.screen_model == "glm-4.6v-flash"
    assert cfg.vision.pricing["glm-4.6v"].output_cny_per_million == 3.0
    assert cfg.sources.allow_mature is False


def test_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.toml")


def test_steam_workshop_section_is_loaded(tmp_path):
    from dailywallpaper.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(
        "[steam_workshop]\nenabled = true\nper_page = 10\nsteam_account = \"me\"\n",
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.steam_workshop.enabled is True
    assert cfg.steam_workshop.per_page == 10
    assert cfg.steam_workshop.steam_account == "me"


def test_a_relative_data_dir_is_resolved_against_the_config_file(tmp_path):
    from dailywallpaper.config import load_config

    path = tmp_path / "c.toml"
    path.write_text('[app]\ndata_dir = "data"\n', encoding="utf-8")

    cfg = load_config(path)

    assert cfg.app.data_dir == str((tmp_path / "data").resolve())
