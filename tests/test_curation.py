import json
from datetime import datetime

from dailywallpaper.curation import seed as seed_mod
from dailywallpaper.db import connect, migrate


def _db(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn


def _wallpaper(conn, source_id="1", source="wallhaven"):
    conn.execute(
        "insert into wallpapers(source,source_id,created_at) values(?,?,'x')",
        (source, source_id),
    )
    conn.commit()
    return conn.execute(
        "select id from wallpapers order by id desc limit 1"
    ).fetchone()[0]


def _analysis(conn, wallpaper_id, tags, mood=()):
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,style_tags_json,mood_tags_json,created_at)"
        " values('glm','v3',?,'{}',80,80,80,10,?,?,'x')",
        (f"h{wallpaper_id}", json.dumps(list(tags)), json.dumps(list(mood))),
    )
    analysis_id = conn.execute("select max(id) from analyses").fetchone()[0]
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
        " values(?,?,'x')",
        (wallpaper_id, analysis_id),
    )
    conn.commit()


def test_build_seed_from_ratings(tmp_path):
    conn = _db(tmp_path)
    good = _wallpaper(conn, "1")
    _analysis(conn, good, ["fog", "negative-space"])
    bad = _wallpaper(conn, "2")
    _analysis(conn, bad, ["neon", "oversaturated"])
    conn.execute(
        "insert into ratings(wallpaper_id,rating,created_at) values(?,10,?)",
        (good, "2026-10-01T10:00:00"),
    )
    conn.execute(
        "insert into ratings(wallpaper_id,rating,created_at) values(?,1,?)",
        (bad, "2026-10-02T10:00:00"),
    )
    conn.commit()

    seed = seed_mod.build_seed(conn)

    assert seed["version"] == 1
    assert seed["rating_count"] == 2
    assert seed["tag_weights"]["fog"] > 0
    assert seed["tag_weights"]["neon"] < 0


def test_seed_round_trips(tmp_path):
    seed = {
        "version": 1,
        "rating_count": 3,
        "tag_weights": {"fog": 0.5},
        "source_weights": {"wallhaven": 0.2},
    }
    path = tmp_path / "seed.json"

    seed_mod.export_seed(seed, path)

    assert seed_mod.load_seed(path) == seed


def test_seed_profile_keeps_the_weights(tmp_path):
    profile = seed_mod.seed_profile(
        {"tag_weights": {"fog": 0.5}, "source_weights": {}, "rating_count": 3}
    )

    assert profile.tag_weights == {"fog": 0.5}
    assert profile.tag_affinity(["fog"]) > 50


def test_a_broken_seed_is_ignored(tmp_path):
    path = tmp_path / "seed.json"
    path.write_text("not json", encoding="utf-8")

    assert seed_mod.load_seed(path) is None


def test_cold_start_uses_the_seed(tmp_path, monkeypatch):
    from dailywallpaper.app import RealPipeline
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
    )

    conn = _db(tmp_path)
    seed_path = tmp_path / "seed.json"
    seed_mod.export_seed(
        {
            "version": 1,
            "rating_count": 5,
            "tag_weights": {"fog": 0.6},
            "source_weights": {},
        },
        seed_path,
    )
    monkeypatch.setattr(seed_mod, "DEFAULT_SEED_PATH", seed_path)
    cfg = Config(
        app=AppSection(data_dir=str(tmp_path)),
        sources=SourcesSection(),
        vision=VisionSection(),
        budget=BudgetSection(),
        weather=WeatherSection(),
        wallpaper=WallpaperSection(),
        wallpaper_engine=WallpaperEngineSection(),
        tools=ToolsSection(),
        retention=RetentionSection(),
    )
    pipeline = RealPipeline(cfg, conn, None, [], None, None)

    profile = pipeline._profile(datetime.now().astimezone())

    assert profile.tag_weights.get("fog") == 0.6
    assert profile.rating_count == 5


def test_seed_profile_reads_avoid_tags():
    profile = seed_mod.seed_profile(
        {
            "tag_weights": {},
            "source_weights": {},
            "rating_count": 1,
            "avoid_tags": ["Anime", "Game-Art"],
        }
    )

    assert profile.avoid_tags == ("anime", "game-art")


def test_seed_profile_reads_boost_tags():
    profile = seed_mod.seed_profile(
        {
            "tag_weights": {},
            "source_weights": {},
            "rating_count": 1,
            "boost_tags": ["Surreal", "Mysterious"],
        }
    )

    assert profile.boost_tags == ("surreal", "mysterious")


def test_ratings_keep_the_seed_rules(tmp_path, monkeypatch):
    from dailywallpaper.app import RealPipeline
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
    )

    conn = _db(tmp_path)
    wid = _wallpaper(conn, "1")
    conn.execute(
        "insert into ratings(wallpaper_id,rating,created_at) values(?,7,?)",
        (wid, "2026-10-01T10:00:00"),
    )
    conn.commit()
    seed_path = tmp_path / "seed.json"
    seed_mod.export_seed(
        {
            "version": 1,
            "rating_count": 5,
            "tag_weights": {"fog": 0.6},
            "source_weights": {},
            "avoid_tags": ["anime"],
            "boost_tags": ["surreal"],
        },
        seed_path,
    )
    monkeypatch.setattr(seed_mod, "DEFAULT_SEED_PATH", seed_path)
    cfg = Config(
        app=AppSection(data_dir=str(tmp_path)),
        sources=SourcesSection(),
        vision=VisionSection(),
        budget=BudgetSection(),
        weather=WeatherSection(),
        wallpaper=WallpaperSection(),
        wallpaper_engine=WallpaperEngineSection(),
        tools=ToolsSection(),
        retention=RetentionSection(),
    )
    pipeline = RealPipeline(cfg, conn, None, [], None, None)

    profile = pipeline._profile(datetime.now().astimezone())

    # The ratings win for weights, but the curator rules still apply.
    assert profile.rating_count == 1
    assert profile.avoid_tags == ("anime",)
    assert profile.boost_tags == ("surreal",)
