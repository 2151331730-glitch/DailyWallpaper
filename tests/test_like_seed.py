# tests/test_like_seed.py
import json
from types import SimpleNamespace

from dailywallpaper.app_support import install_allows_like_seed
from dailywallpaper.curation.like_seed import (
    SEED_RATING,
    maybe_seed_subscribed_likes,
    seed_liked_subscriptions,
)
from dailywallpaper.db import connect, migrate


def _db(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn


def _project(root, item_id="1781466079", name="clip.mp4", rating="Everyone"):
    """One subscribed item, shaped like a real Wallpaper Engine download."""
    folder = root / item_id
    folder.mkdir(parents=True)
    (folder / name).write_bytes(b"video")
    (folder / "preview.jpg").write_bytes(b"jpg")
    (folder / "project.json").write_text(
        json.dumps(
            {
                "type": "video",
                "file": name,
                "preview": "preview.jpg",
                "title": "合成壁纸",
                "contentrating": rating,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return folder


def _prefs(monkeypatch, **values):
    from dailywallpaper import config

    monkeypatch.setattr(config, "install_prefs", lambda: dict(values))


def test_every_subscribed_project_is_marked_as_liked(tmp_path):
    root = tmp_path / "431960"
    _project(root, "1781466079")
    _project(root, "1953370306", name="clip2.mp4")
    conn = _db(tmp_path)

    added = seed_liked_subscriptions(conn, root)

    assert added == 2
    rows = conn.execute(
        "select w.source, w.source_id, r.rating from ratings r"
        " join wallpapers w on w.id = r.wallpaper_id order by w.source_id"
    ).fetchall()
    assert [row["source_id"] for row in rows] == ["1781466079", "1953370306"]
    assert all(row["source"] == "we_workshop" for row in rows)
    assert all(row["rating"] == SEED_RATING for row in rows)


def test_a_rating_the_user_already_made_is_never_overwritten(tmp_path):
    # A 1-2 bans the wallpaper; seeding a like over it would put a picture the
    # user rejected back into every pool.
    root = tmp_path / "431960"
    banned = _project(root, "1111111111")
    _project(root, "2222222222", name="other.mp4")
    conn = _db(tmp_path)
    seed_liked_subscriptions(conn, root)
    conn.execute(
        "update ratings set rating=2 where wallpaper_id in"
        " (select id from wallpapers where source_id=?)",
        (banned.name,),
    )
    conn.commit()

    added = seed_liked_subscriptions(conn, root)

    assert added == 0
    kept = conn.execute(
        "select r.rating from ratings r join wallpapers w on w.id=r.wallpaper_id"
        " where w.source_id=?",
        (banned.name,),
    ).fetchone()
    assert kept["rating"] == 2


def test_running_twice_leaves_one_rating_per_wallpaper(tmp_path):
    root = tmp_path / "431960"
    _project(root)
    conn = _db(tmp_path)

    seed_liked_subscriptions(conn, root)
    again = seed_liked_subscriptions(conn, root)

    assert again == 0
    assert conn.execute("select count(*) from ratings").fetchone()[0] == 1


def test_mature_items_are_included_even_when_the_pool_hides_them(tmp_path):
    # Hiding mature items from the daily pool is a separate setting; a
    # subscription is still the user saying "I like this".
    root = tmp_path / "431960"
    _project(root, "3333333333", rating="Mature")
    conn = _db(tmp_path)

    assert seed_liked_subscriptions(conn, root) == 1


def test_the_new_subscribed_like_pref_is_off_unless_the_installer_said_so(monkeypatch):
    _prefs(monkeypatch)
    assert install_allows_like_seed() is False

    _prefs(monkeypatch, seed_subscribed_likes=True)
    assert install_allows_like_seed() is True


def _cfg(install_dir):
    return SimpleNamespace(
        wallpaper_engine=SimpleNamespace(install_dir=str(install_dir))
    )


def test_the_startup_hook_seeds_from_the_wallpaper_engine_folder(tmp_path, monkeypatch):
    steamapps = tmp_path / "steamapps"
    install = steamapps / "common" / "wallpaper_engine"
    _project(steamapps / "workshop" / "content" / "431960")
    conn = _db(tmp_path)
    _prefs(monkeypatch, seed_subscribed_likes=True)

    assert maybe_seed_subscribed_likes(_cfg(install), conn) == 1


def test_the_startup_hook_does_nothing_without_the_installer_choice(tmp_path, monkeypatch):
    steamapps = tmp_path / "steamapps"
    install = steamapps / "common" / "wallpaper_engine"
    _project(steamapps / "workshop" / "content" / "431960")
    conn = _db(tmp_path)
    _prefs(monkeypatch)

    assert maybe_seed_subscribed_likes(_cfg(install), conn) == 0
    assert conn.execute("select count(*) from ratings").fetchone()[0] == 0


def test_the_startup_hook_survives_a_missing_library(tmp_path, monkeypatch):
    # Steam not installed yet: the next launch, after setup, still needs to
    # work, so this must be a quiet zero rather than an exception.
    conn = _db(tmp_path)
    _prefs(monkeypatch, seed_subscribed_likes=True)

    assert maybe_seed_subscribed_likes(_cfg(""), conn) == 0
    assert maybe_seed_subscribed_likes(_cfg(tmp_path / "nope"), conn) == 0
