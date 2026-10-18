# tests/test_steam_prefetch.py
from pathlib import Path

from dailywallpaper.db import connect, migrate
from dailywallpaper.steam_prefetch import Prefetcher


def _conn(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn


def _remote(conn, file_id, quality, root):
    conn.execute(
        "insert into wallpapers(source,source_id,file_url,media_type,created_at)"
        " values('steam_workshop',?,?,'scene','x')",
        (file_id, str(root / file_id / "project.json")),
    )
    wid = conn.execute("select id from wallpapers where source_id=?", (file_id,)).fetchone()[0]
    cursor = conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,nsfw,style_tags_json,mood_tags_json,palette_json,"
        "tokens_in,tokens_out,cost_cny,created_at)"
        " values('m','v1',?, '{}', ?, 0, 0, 0, 0, '[]', '[]', '[]', 0, 0, 0, 'x')",
        (f"hash-{file_id}", quality),
    )
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at) values(?,?, 'x')",
        (wid, cursor.lastrowid),
    )
    conn.commit()


class FakeSteamCmd:
    def __init__(self, install_root: Path, fail=()):
        self.install_root = install_root
        self.fail = set(fail)
        self.calls = []

    def download_item(self, account, file_id, install_dir):
        self.calls.append(file_id)
        if file_id in self.fail:
            from dailywallpaper.steamcmd import SteamCmdError

            raise SteamCmdError(f"{file_id} failed")
        folder = Path(install_dir) / "steamapps" / "workshop" / "content" / "431960" / file_id
        folder.mkdir(parents=True)
        project = folder / "project.json"
        project.write_text("{}", encoding="utf-8")
        return project


def _prefetcher(tmp_path, fake, top_n=2, max_cache_gb=3.0):
    root = tmp_path / "steam_workshop"
    return Prefetcher(
        conn_factory=lambda: connect(tmp_path / "app.db"),
        download_root=root,
        steamcmd=fake,
        account="me",
        top_n=top_n,
        max_cache_gb=max_cache_gb,
        staging=root / "_steamcmd",
    )


def test_the_best_missing_item_is_downloaded(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "steam_workshop"
    _remote(conn, "111", 95, root)
    _remote(conn, "222", 70, root)
    fake = FakeSteamCmd(root)
    prefetcher = _prefetcher(tmp_path, fake, top_n=1)

    downloaded = prefetcher.run_once()

    assert downloaded == ["111"]
    assert (root / "111" / "project.json").is_file()
    assert not (root / "222").exists()


def test_a_ready_item_is_not_downloaded_again(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "steam_workshop"
    _remote(conn, "111", 95, root)
    (root / "111").mkdir(parents=True)
    (root / "111" / "project.json").write_text("{}", encoding="utf-8")
    fake = FakeSteamCmd(root)
    prefetcher = _prefetcher(tmp_path, fake)

    assert prefetcher.run_once() == []
    assert fake.calls == []


def test_one_failed_download_does_not_stop_the_next(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "steam_workshop"
    _remote(conn, "111", 95, root)
    _remote(conn, "222", 70, root)
    fake = FakeSteamCmd(root, fail={"111"})
    prefetcher = _prefetcher(tmp_path, fake)

    downloaded = prefetcher.run_once()

    assert downloaded == ["222"]
    assert (root / "222" / "project.json").is_file()


def test_old_downloads_are_evicted(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "steam_workshop"
    _remote(conn, "111", 95, root)
    _remote(conn, "222", 70, root)
    # An old item with no analysis row: it is only cache, not a candidate.
    old = root / "999"
    old.mkdir(parents=True)
    (old / "project.json").write_text("{}", encoding="utf-8")
    (old / "blob.bin").write_bytes(b"x" * 2048)
    fake = FakeSteamCmd(root)
    prefetcher = _prefetcher(tmp_path, fake, top_n=0, max_cache_gb=0.000001)

    prefetcher.run_once()

    assert not old.exists()


def test_the_last_applied_item_is_never_evicted(tmp_path):
    conn = _conn(tmp_path)
    root = tmp_path / "steam_workshop"
    _remote(conn, "111", 95, root)
    current = root / "111"
    current.mkdir(parents=True)
    (current / "project.json").write_text("{}", encoding="utf-8")
    (current / "blob.bin").write_bytes(b"x" * 2048)
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,previous_wallpaper,"
        "previous_we_config_json,method,applied_at,success)"
        " values('2026-10-18',1,'','{}','we','x',1)"
    )
    conn.commit()
    prefetcher = _prefetcher(tmp_path, FakeSteamCmd(root), top_n=0, max_cache_gb=0.000001)

    prefetcher.run_once()

    assert current.exists()


def test_build_prefetcher_needs_an_account_and_steamcmd(tmp_path):
    from dataclasses import replace

    from dailywallpaper.config import (
        AppSection, BudgetSection, Config, RetentionSection, SourcesSection,
        SteamWorkshopSection, ToolsSection, VisionSection, WallpaperEngineSection,
        WallpaperSection, WeatherSection,
    )
    from dailywallpaper.steam_prefetch import build_prefetcher

    cfg = Config(
        AppSection(data_dir=str(tmp_path)), SourcesSection(), VisionSection(),
        BudgetSection(), WeatherSection(), WallpaperSection(),
        WallpaperEngineSection(), ToolsSection(), RetentionSection(),
        SteamWorkshopSection(enabled=True),
    )
    assert build_prefetcher(cfg, lambda: connect(tmp_path / "app.db")) is None

    cmd = tmp_path / "steamcmd.exe"
    cmd.write_bytes(b"x")
    ready = replace(
        cfg,
        steam_workshop=replace(
            cfg.steam_workshop, steam_account="me", steamcmd_path=str(cmd)
        ),
    )
    assert build_prefetcher(ready, lambda: connect(tmp_path / "app.db")) is not None


def test_a_missing_cache_directory_is_not_an_error(tmp_path):
    _conn(tmp_path)
    root = tmp_path / "steam_workshop"

    prefetcher = _prefetcher(tmp_path, FakeSteamCmd(root))

    assert prefetcher.run_once() == []
