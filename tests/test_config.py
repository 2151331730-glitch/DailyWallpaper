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
