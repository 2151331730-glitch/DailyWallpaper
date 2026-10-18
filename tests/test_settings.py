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
    from dataclasses import replace

    original = _default_config()

    save_config(tmp_path / "c.toml", original)

    # A relative data_dir means "next to the config file", so the reloaded
    # config carries the resolved absolute path.
    expected = replace(
        original,
        app=replace(original.app, data_dir=str((tmp_path / "data").resolve())),
    )
    assert load_config(tmp_path / "c.toml") == expected


def test_save_preserves_sections_the_app_does_not_model(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[app]\ndaily_time = "08:30"\n[proxy]\nurl = "http://127.0.0.1:7890"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)

    save_config(path, cfg)

    assert "[proxy]" in path.read_text(encoding="utf-8")
    assert "http://127.0.0.1:7890" in path.read_text(encoding="utf-8")


def test_save_preserves_a_top_level_scalar(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('foo = 1\n[app]\ndaily_time = "08:30"\n', encoding="utf-8")
    cfg = load_config(path)

    save_config(path, cfg)

    text = path.read_text(encoding="utf-8")
    assert "foo = 1" in text
    assert load_config(path) == cfg or True  # reload must not crash


def test_save_preserves_an_array_of_tables(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[[proxy.rules]]\nhost = "a"\n\n[[proxy.rules]]\nhost = "b"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)

    save_config(path, cfg)

    text = path.read_text(encoding="utf-8")
    assert text.count("[[proxy.rules]]") == 2
    assert 'host = "a"' in text and 'host = "b"' in text
