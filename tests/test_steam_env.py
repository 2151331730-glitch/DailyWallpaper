# tests/test_steam_env.py
from pathlib import Path


from dailywallpaper import steam_env
from dailywallpaper.config import load_config


def _cfg(tmp_path, install_dir: str = "", auto_detect: bool | None = None) -> object:
    lines = [f'install_dir = "{install_dir}"']
    if auto_detect is not None:
        lines.append(f"auto_detect = {str(auto_detect).lower()}")
    path = tmp_path / "c.toml"
    path.write_text("[wallpaper_engine]\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return load_config(path)


def _make_library(root: Path, *, with_we: bool, with_manifest: bool) -> Path:
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    if with_manifest:
        (steamapps / "appmanifest_431960.acf").write_text('"AppState" {}')
    if with_we:
        we = steamapps / "common" / "wallpaper_engine"
        we.mkdir(parents=True)
        (we / "wallpaper64.exe").write_text("")
        (steamapps / "workshop" / "content" / "431960").mkdir(parents=True)
    return root


def test_library_folders_reads_the_current_layout(tmp_path):
    root = _make_library(tmp_path / "Steam", with_we=False, with_manifest=False)
    extra = tmp_path / "OtherLibrary"
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n'
        '\t"0"\n\t{\n\t\t"path"\t\t"C:\\\\Steam"\n\t}\n'
        f'\t"1"\n\t{{\n\t\t"path"\t\t"{str(extra).replace(chr(92), chr(92) * 2)}"\n\t}}\n'
        "}\n",
        encoding="utf-8",
    )

    folders = steam_env.library_folders(root)

    assert root in folders
    assert extra in folders


def test_library_folders_reads_the_legacy_layout(tmp_path):
    root = _make_library(tmp_path / "Steam", with_we=False, with_manifest=False)
    legacy = tmp_path / "Legacy"
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        '"LibraryFolders"\n{\n'
        '\t"TimeNextStatsReport"\t"1"\n'
        f'\t"1"\t\t"{str(legacy).replace(chr(92), chr(92) * 2)}"\n'
        "}\n",
        encoding="utf-8",
    )

    assert legacy in steam_env.library_folders(root)


def test_detect_finds_wallpaper_engine_in_a_second_library(tmp_path, monkeypatch):
    root = _make_library(tmp_path / "Steam", with_we=False, with_manifest=True)
    extra = tmp_path / "Games"
    _make_library(extra, with_we=True, with_manifest=True)
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        f'\t"1"\n\t{{\n\t\t"path"\t\t"{str(extra).replace(chr(92), chr(92) * 2)}"\n\t}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(steam_env, "find_steam_root", lambda: root)

    env = steam_env.detect()

    assert env.we_owned is True
    assert env.we_install_dir == extra / "steamapps" / "common" / "wallpaper_engine"
    assert env.workshop_dir == extra / "steamapps" / "workshop" / "content" / "431960"
    assert env.usable is True


def test_detect_without_steam_is_empty(monkeypatch):
    monkeypatch.setattr(steam_env, "find_steam_root", lambda: None)

    env = steam_env.detect()

    assert env.steam_root is None
    assert env.usable is False


def test_apply_to_config_fills_the_detected_path(tmp_path, monkeypatch):
    library = _make_library(tmp_path / "Steam", with_we=True, with_manifest=True)
    monkeypatch.setattr(steam_env, "find_steam_root", lambda: library)
    cfg = _cfg(tmp_path)

    updated = steam_env.apply_to_config(cfg)

    we = library / "steamapps" / "common" / "wallpaper_engine"
    assert updated.wallpaper_engine.install_dir == str(we)
    assert updated.wallpaper_engine.config_path == str(we / "config.json")
    assert updated.sources.we_workshop_enabled is True


def test_apply_to_config_keeps_a_working_user_path(tmp_path, monkeypatch):
    library = _make_library(tmp_path / "Steam", with_we=True, with_manifest=True)
    monkeypatch.setattr(steam_env, "find_steam_root", lambda: library)
    chosen = tmp_path / "Portable"
    chosen.mkdir()
    (chosen / "wallpaper64.exe").write_text("")
    cfg = _cfg(tmp_path, install_dir=str(chosen).replace("\\", "\\\\"))

    updated = steam_env.apply_to_config(cfg)

    assert updated.wallpaper_engine.install_dir == str(chosen)


def test_an_empty_folder_does_not_suppress_detection(tmp_path, monkeypatch):
    library = _make_library(tmp_path / "Steam", with_we=True, with_manifest=True)
    monkeypatch.setattr(steam_env, "find_steam_root", lambda: library)
    empty = tmp_path / "Empty"
    empty.mkdir()
    cfg = _cfg(tmp_path, install_dir=str(empty).replace("\\", "\\\\"))

    updated = steam_env.apply_to_config(cfg)

    installed = library / "steamapps" / "common" / "wallpaper_engine"
    assert updated.wallpaper_engine.install_dir == str(installed)


def test_auto_detect_off_leaves_the_config_alone(tmp_path, monkeypatch):
    library = _make_library(tmp_path / "Steam", with_we=True, with_manifest=True)
    monkeypatch.setattr(steam_env, "find_steam_root", lambda: library)
    cfg = _cfg(tmp_path, auto_detect=False)

    updated = steam_env.apply_to_config(cfg)

    assert updated.wallpaper_engine.install_dir == ""
    assert updated.sources.we_workshop_enabled is False


def test_a_32_bit_only_folder_is_not_usable(tmp_path, monkeypatch):
    library = _make_library(tmp_path / "Steam", with_we=True, with_manifest=True)
    we = library / "steamapps" / "common" / "wallpaper_engine"
    (we / "wallpaper64.exe").unlink()
    (we / "wallpaper32.exe").write_text("")
    monkeypatch.setattr(steam_env, "find_steam_root", lambda: library)

    env = steam_env.detect()

    # WeControl drives wallpaper64.exe, so a 32-bit folder must not be offered.
    assert env.we_install_dir is None


def test_workshop_source_turns_on_for_an_already_configured_path(tmp_path, monkeypatch):
    library = _make_library(tmp_path / "Steam", with_we=True, with_manifest=True)
    monkeypatch.setattr(steam_env, "find_steam_root", lambda: library)
    we = library / "steamapps" / "common" / "wallpaper_engine"
    cfg = _cfg(tmp_path, install_dir=str(we).replace("\\", "\\\\"))
    assert cfg.sources.we_workshop_enabled is False

    updated = steam_env.apply_to_config(cfg)

    # The path was already saved; a workshop subscription appeared later.
    assert updated.wallpaper_engine.install_dir == str(we)
    assert updated.sources.we_workshop_enabled is True


def test_a_dead_configured_proxy_falls_back_to_direct(monkeypatch):
    monkeypatch.setattr("dailywallpaper.net.proxy_alive", lambda text, timeout=0.5: False)

    assert steam_env._live_proxies("127.0.0.1:7892") is None


def test_check_sites_reports_each_site(monkeypatch):
    seen = []

    def fake_probe(url, proxies, timeout):
        seen.append(url)
        return "store" in url

    monkeypatch.setattr(steam_env, "_probe", fake_probe)

    status = steam_env.check_sites(timeout=0.1)

    assert status["Steam 商店"] is True
    assert status["Steam 社区 / 创意工坊"] is False
    assert len(seen) == 2
