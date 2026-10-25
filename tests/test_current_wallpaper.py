# tests/test_current_wallpaper.py
"""The rating target must be the wallpaper actually on the desktop."""

from dailywallpaper.apply.service import ApplyService
from dailywallpaper.db import connect, migrate


class _Applier:
    def __init__(self, current=""):
        self.current = current

    def current_wallpaper(self):
        return self.current


class _We:
    def __init__(self, path=""):
        self.path = path

    def current(self):
        return self.path


def _db(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn


def _wallpaper(conn, *, file_url="", local_path="", media_type="image",
               source="we_workshop", source_id="1"):
    conn.execute(
        "insert into wallpapers(source,source_id,title,local_path,file_url,"
        "media_type,created_at) values(?,?,?,?,?,?,'x')",
        (source, source_id, "t", local_path, file_url, media_type),
    )
    conn.commit()
    return conn.execute("select max(id) from wallpapers").fetchone()[0]


def _decision(conn, wallpaper_id, score=70):
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-19',?,70,70,?, 'r','applied','x')",
        (wallpaper_id, score),
    )
    conn.commit()


def test_static_desktop_resolves_by_the_apply_file_name(tmp_path):
    conn = _db(tmp_path)
    wid = _wallpaper(conn, source="bing", source_id="b")
    service = ApplyService(
        conn, _Applier(str(tmp_path / "wallpapers" / f"{wid}.apply.jpg"))
    )

    assert service.current_wallpaper_id() == wid


def test_static_desktop_resolves_by_the_recorded_local_path(tmp_path):
    conn = _db(tmp_path)
    local = tmp_path / "cache" / "normalized" / "abc.jpg"
    wid = _wallpaper(conn, source="bing", source_id="b", local_path=str(local))
    service = ApplyService(conn, _Applier(str(local.with_suffix(".apply.jpg"))))

    assert service.current_wallpaper_id() == wid


def test_wallpaper_engine_resolves_by_project_folder(tmp_path):
    conn = _db(tmp_path)
    folder = tmp_path / "steam_workshop" / "3796181648"
    wid = _wallpaper(
        conn,
        source="steam_workshop",
        source_id="3796181648",
        file_url=str(folder / "project.json"),
        media_type="video",
    )
    service = ApplyService(conn, _Applier(), we=_We(str(folder / "live.mp4")))

    assert service.current_wallpaper_id() == wid


def test_we_wins_over_a_stale_static_wallpaper(tmp_path):
    conn = _db(tmp_path)
    stale = _wallpaper(conn, source="bing", source_id="old")
    folder = tmp_path / "workshop" / "42"
    live = _wallpaper(
        conn,
        source="we_workshop",
        source_id="42",
        file_url=str(folder / "scene.pkg"),
        media_type="scene",
    )
    service = ApplyService(
        conn,
        _Applier(str(tmp_path / "wallpapers" / f"{stale}.apply.jpg")),
        we=_We(str(folder / "scene.pkg")),
    )

    assert service.current_wallpaper_id() == live


def _change(conn, wallpaper_id, previous_we_file):
    import json

    conn.execute(
        "insert into change_log(change_date,wallpaper_id,previous_wallpaper,"
        "previous_we_config_json,method,applied_at,success)"
        " values('2026-10-20',?,'',?,'manual','2026-10-20T12:00:00',1)",
        (wallpaper_id, json.dumps({"file": previous_we_file})),
    )
    conn.commit()


def test_a_static_apply_wins_over_the_we_config_it_closed(tmp_path):
    conn = _db(tmp_path)
    folder = tmp_path / "workshop" / "42"
    live_file = str(folder / "scene.pkg")
    live = _wallpaper(
        conn, source="we_workshop", source_id="42",
        file_url=live_file, media_type="scene",
    )
    fresh = _wallpaper(conn, source="bing", source_id="b")
    # The static apply closed WE; its config still names the live wallpaper.
    _change(conn, fresh, live_file)
    service = ApplyService(
        conn,
        _Applier(str(tmp_path / "wallpapers" / f"{fresh}.apply.jpg")),
        we=_We(live_file),
    )

    assert service.current_wallpaper_id() == fresh


def test_an_undone_apply_is_not_the_current_wallpaper(tmp_path):
    conn = _db(tmp_path)
    folder = tmp_path / "workshop" / "42"
    live_file = str(folder / "scene.pkg")
    live = _wallpaper(
        conn, source="we_workshop", source_id="42",
        file_url=live_file, media_type="scene",
    )
    fresh = _wallpaper(conn, source="bing", source_id="b")
    _change(conn, fresh, live_file)
    # 撤销上一张 reopened the live wallpaper after the static apply.
    conn.execute(
        "insert or replace into state(key,value) values('last_undo_at',?)",
        ("2026-10-20T12:30:00",),
    )
    conn.commit()
    service = ApplyService(
        conn,
        _Applier(str(tmp_path / "wallpapers" / f"{fresh}.apply.jpg")),
        we=_We(live_file),
    )

    assert service.current_wallpaper_id() == live


def test_a_we_switch_after_the_apply_still_wins(tmp_path):
    conn = _db(tmp_path)
    folder = tmp_path / "workshop" / "42"
    live_file = str(folder / "scene.pkg")
    live = _wallpaper(
        conn, source="we_workshop", source_id="42",
        file_url=live_file, media_type="scene",
    )
    fresh = _wallpaper(conn, source="bing", source_id="b")
    # The user picked a different wallpaper inside WE after our apply.
    _change(conn, fresh, str(tmp_path / "workshop" / "99" / "old.pkg"))
    service = ApplyService(
        conn,
        _Applier(str(tmp_path / "wallpapers" / f"{fresh}.apply.jpg")),
        we=_We(live_file),
    )

    assert service.current_wallpaper_id() == live


def test_unknown_we_wallpaper_returns_no_match(tmp_path):
    conn = _db(tmp_path)
    _wallpaper(conn, source="bing", source_id="b")
    service = ApplyService(conn, _Applier(), we=_We(str(tmp_path / "other" / "x.mp4")))

    assert service.current_wallpaper_id() is None


def test_a_folder_with_two_rows_prefers_the_newest_decision(tmp_path):
    conn = _db(tmp_path)
    folder = tmp_path / "workshop" / "42"
    old = _wallpaper(
        conn, source="we_workshop", source_id="42-old",
        file_url=str(folder / "project.json"), media_type="video",
    )
    new = _wallpaper(
        conn, source="steam_workshop", source_id="42-new",
        file_url=str(folder / "project.json"), media_type="video",
    )
    _decision(conn, old)
    _decision(conn, new)
    service = ApplyService(conn, _Applier(), we=_We(str(folder / "v.mp4")))

    assert service.current_wallpaper_id() == new


def test_the_we_match_cache_refreshes_when_the_table_changes(tmp_path):
    conn = _db(tmp_path)
    folder = tmp_path / "workshop" / "42"
    first = _wallpaper(
        conn, source="we_workshop", source_id="42-old",
        file_url=str(folder / "project.json"), media_type="video",
    )
    service = ApplyService(conn, _Applier(), we=_We(str(folder / "v.mp4")))

    assert service._match_we(str(folder / "v.mp4")) == first

    second = _wallpaper(
        conn, source="steam_workshop", source_id="42-new",
        file_url=str(folder / "project.json"), media_type="video",
    )
    _decision(conn, second)

    assert service._match_we(str(folder / "v.mp4")) == second
