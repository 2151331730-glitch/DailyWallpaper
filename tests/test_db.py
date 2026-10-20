# tests/test_db.py
import re
import sqlite3

import pytest

from dailywallpaper.db import backup, connect, migrate, now_iso, today_iso


def test_schema_created(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"wallpapers", "analyses", "wallpaper_analyses", "decisions", "change_log",
            "ratings", "api_usage", "state"} <= tables


def test_one_automatic_success_per_day(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    row = dict(change_date="2026-10-17", wallpaper_id=wid, method="static", applied_at="x", success=1)
    conn.execute("insert into change_log(change_date,wallpaper_id,method,applied_at,success) values(:change_date,:wallpaper_id,:method,:applied_at,:success)", row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into change_log(change_date,wallpaper_id,method,applied_at,success) values(:change_date,:wallpaper_id,:method,:applied_at,:success)", row)


def test_manual_success_does_not_block_automatic(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute("insert into change_log(change_date,wallpaper_id,method,applied_at,success) values('2026-10-17',?,'manual','x',1)", (wid,))
    conn.execute("insert into change_log(change_date,wallpaper_id,method,applied_at,success) values('2026-10-17',?,'static','x',1)", (wid,))
    assert conn.execute("select count(*) from change_log").fetchone()[0] == 2


def test_backup(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    dest = backup(conn, tmp_path / "backup.db")
    assert dest.exists()


def test_v1_three_state_ratings_become_ten_point_scores(tmp_path):
    from dailywallpaper.db import SCHEMA_SQL, SCHEMA_VERSION

    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript(SCHEMA_SQL)
    old.execute("PRAGMA user_version=1")
    for index, rating in enumerate((-1, 0, 1)):
        old.execute(
            "insert into wallpapers(source,source_id,created_at) values('t',?,'x')",
            (str(index),),
        )
        wid = old.execute("select last_insert_rowid()").fetchone()[0]
        old.execute(
            "insert into ratings(wallpaper_id,rating,comment,derived_tags_json,"
            "created_at) values(?,?,'','[]','x')",
            (wid, rating),
        )
    old.commit()
    old.close()

    conn = connect(tmp_path / "old.db")
    migrate(conn)

    ratings = [row[0] for row in conn.execute("select rating from ratings order by id")]
    assert ratings == [1, 5, 10]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_now_iso_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", now_iso())


def test_today_iso_format():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", today_iso())
