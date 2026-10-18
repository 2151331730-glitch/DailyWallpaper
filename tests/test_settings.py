# tests/test_settings.py
from dailywallpaper.config import (
    AppSection,
    BudgetSection,
    Config,
    RetentionSection,
    SourcesSection,
    ToolsSection,
    VisionSection,
    WallpaperEngineSection,
    WallpaperSection,
    WeatherSection,
    load_config,
    save_config,
)


def _default_config():
    return Config(
        AppSection(), SourcesSection(), VisionSection(), BudgetSection(),
        WeatherSection(), WallpaperSection(), WallpaperEngineSection(),
        ToolsSection(), RetentionSection(),
    )


def test_round_trip_is_lossless(tmp_path):
    source = tmp_path / "in.toml"
    source.write_text(
        '[app]\ndata_dir = "data"\ndaily_time = "07:45"\nstart_with_windows = true\n'
        '[budget]\nmonthly_cny = 12.5\n'
        '[vision]\nscreen_model = "glm-x"\n'
        '[vision.pricing."glm-x"]\ninput_cny_per_million = 1.5\noutput_cny_per_million = 2.5\n',
        encoding="utf-8",
    )
    original = load_config(source)

    save_config(tmp_path / "out.toml", original)
    reloaded = load_config(tmp_path / "out.toml")

    assert reloaded == original


def test_saved_file_is_marked_as_generated(tmp_path):
    save_config(tmp_path / "c.toml", _default_config())

    assert "by the app" in (tmp_path / "c.toml").read_text(encoding="utf-8").splitlines()[0]


def test_saved_defaults_reload_unchanged(tmp_path):
    original = _default_config()

    save_config(tmp_path / "c.toml", original)

    assert load_config(tmp_path / "c.toml") == original
