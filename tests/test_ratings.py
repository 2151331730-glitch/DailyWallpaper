# tests/test_ratings.py
import pytest

from dailywallpaper.db import connect, migrate
from dailywallpaper.recommend.ratings import RatingsService


def _setup(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,title,local_path,created_at)"
        " values('bing','1','测试壁纸','C:/x.jpg','x')"
    )
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    return conn, wid


def test_first_rating_is_inserted(tmp_path):
    conn, wid = _setup(tmp_path)
    service = RatingsService(conn)

    service.rate(wid, 1)

    row = conn.execute("select rating, comment from ratings").fetchone()
    assert row["rating"] == 1
    assert service.rating_for(wid) == 1


def test_second_rating_replaces_the_first(tmp_path):
    conn, wid = _setup(tmp_path)
    service = RatingsService(conn)

    service.rate(wid, 1)
    service.rate(wid, -1, comment="太暗")

    rows = conn.execute("select rating, comment from ratings").fetchall()
    assert len(rows) == 1
    assert rows[0]["rating"] == -1
    assert rows[0]["comment"] == "太暗"


def test_rating_without_an_analysis_stores_empty_tags(tmp_path):
    conn, wid = _setup(tmp_path)
    RatingsService(conn).rate(wid, 1)

    assert conn.execute("select derived_tags_json from ratings").fetchone()[0] == "[]"


def test_rating_inherits_the_primary_style_tag(tmp_path):
    conn, wid = _setup(tmp_path)
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,style_tags_json,created_at)"
        " values('m','v1','h','{}',80,70,60,10,'[\"landscape\",\"calm\"]','x')"
    )
    aid = conn.execute("select id from analyses").fetchone()[0]
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at) values(?,?,?)",
        (wid, aid, "x"),
    )
    conn.commit()

    RatingsService(conn).rate(wid, 1)

    assert conn.execute("select derived_tags_json from ratings").fetchone()[0] == '["landscape"]'


def test_comment_is_capped_at_200_characters(tmp_path):
    conn, wid = _setup(tmp_path)
    RatingsService(conn).rate(wid, 1, comment="字" * 300)

    assert len(conn.execute("select comment from ratings").fetchone()[0]) == 200


def test_rating_is_clamped_to_the_three_allowed_values(tmp_path):
    conn, wid = _setup(tmp_path)
    service = RatingsService(conn)

    with pytest.raises(ValueError):
        service.rate(wid, 7)


def test_history_returns_the_latest_decisions(tmp_path):
    conn, wid = _setup(tmp_path)
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-18',?,'70','80','74.5','r','applied','x')",
        (wid,),
    )
    conn.commit()
    RatingsService(conn).rate(wid, 1)

    rows = RatingsService(conn).history()

    assert len(rows) == 1
    assert rows[0].title == "测试壁纸"
    assert rows[0].rating == 1
    assert rows[0].final_score == pytest.approx(74.5)


def test_history_includes_unrated_entries(tmp_path):
    conn, wid = _setup(tmp_path)
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-18',?,'70','80','74.5','r','applied','x')",
        (wid,),
    )
    conn.commit()

    assert RatingsService(conn).history()[0].rating is None
