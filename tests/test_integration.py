# tests/test_integration.py
import io
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image

from dailywallpaper.analysis.prompts import PROMPT_VERSION
from dailywallpaper.analysis.vision import (
    VisionCallResult,
    VisionClient,
    VisionError,
    VisionScores,
)
from dailywallpaper.app import RealPipeline
from dailywallpaper.apply.service import ApplyService
from dailywallpaper.apply.windows_static import ApplyResult, WallpaperState
from dailywallpaper.config import load_config
from dailywallpaper.cost.budget import BudgetGuard
from dailywallpaper.db import connect, migrate
from dailywallpaper.sources.base import Candidate
from dailywallpaper.state.machine import DailyStateMachine

MODEL = "glm-4.6v-flash"


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment

    def advance(self, days):
        self.moment += timedelta(days=days)


def _config(tmp_path, review_top_n=0):
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
[app]
data_dir = "{(tmp_path / 'data').as_posix()}"
daily_time = "08:30"
latest_change_time = "22:00"

[sources]
wallhaven_enabled = true
bing_enabled = false

[vision]
screen_model = "{MODEL}"
review_model = "review-model"
review_top_n = {review_top_n}

[vision.pricing."{MODEL}"]
input_cny_per_million = 1.0
output_cny_per_million = 2.0

[vision.pricing."review-model"]
input_cny_per_million = 1.0
output_cny_per_million = 2.0

[budget]
monthly_cny = 10.0
""",
        encoding="utf-8",
    )
    return load_config(path)


def _checkerboard(cell):
    # Two mid-grey tones: sharp enough to pass the blur check, and no pixel
    # is dark enough to trip the black-bar check.
    bands = ((np.indices((320, 512)).sum(axis=0) // cell) % 2)
    arr = np.where(bands == 0, 120, 200).astype(np.uint8)
    return Image.fromarray(np.stack([arr] * 3, axis=-1))


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, size):
        return iter([self.payload])


class ImageSession:
    def __init__(self, broken=()):
        self.calls = 0
        self.images = {}
        for index, cell in enumerate((8, 16, 32)):
            buffer = io.BytesIO()
            _checkerboard(cell).save(buffer, "PNG")
            self.images[f"https://img/{index}.png"] = buffer.getvalue()
        for url in broken:
            self.images.pop(url, None)

    def get(self, url, stream=True, timeout=30):
        self.calls += 1
        if url not in self.images:
            raise RuntimeError(f"404 for {url}")
        return _Response(self.images[url])


class Source:
    def __init__(self, candidates):
        self.candidates = candidates

    def fetch(self, limit=None):
        return list(self.candidates)


class RateLimitedProvider:
    """Every call is refused the way Zhipu refuses an overloaded model."""

    def __init__(self):
        self.calls = 0

    def analyze(self, image_path, model, extra_instruction=""):
        self.calls += 1
        raise RuntimeError(
            "Error code: 429 - {'error': {'code': '1305', 'message':"
            " '该模型当前访问量过大，请您稍后再试'}}"
        )


class DaySource:
    """A source that serves fresh candidates on every fetch, like a real one."""

    def __init__(self):
        self.day = 0

    def fetch(self, limit=None):
        self.day += 1
        return [
            Candidate(
                "wallhaven", f"d{self.day}-{index}", "t", "p", "f",
                f"https://img/day{self.day}/{index}.png",
                "image", 3840, 2160, 1000, [], {},
            )
            for index in range(3)
        ]


class _AcPower:
    """Deterministic AC power.

    ``RealPipeline`` asks the host for its power state when
    ``dynamic_requires_ac`` is on, so a laptop running these tests on battery
    would silently turn every "dynamic day" test into a static one. Tests
    that care about battery pass their own stub instead.
    """

    def on_battery(self):
        return False


class SynthSession:
    """Serve a distinct, sharp, mid-tone image for any https://img/... URL."""

    def __init__(self):
        self.calls = 0

    def get(self, url, stream=True, timeout=30):
        import hashlib

        self.calls += 1
        seed = int(hashlib.sha256(url.encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        blocks = rng.integers(60, 220, size=(10, 16)).astype(np.uint8)
        arr = np.kron(blocks, np.ones((32, 32), dtype=np.uint8))
        buffer = io.BytesIO()
        Image.fromarray(np.stack([arr] * 3, axis=-1)).save(buffer, "PNG")
        return _Response(buffer.getvalue())


class BigSession:
    """Serves one 2560x1440 sharp image for any URL, so inspection passes."""

    def __init__(self):
        import io

        buffer = io.BytesIO()
        _checkerboard(8).resize((2560, 1440), Image.Resampling.NEAREST).save(
            buffer, "PNG"
        )
        self.payload = buffer.getvalue()

    def get(self, url, stream=True, timeout=30):
        return _Response(self.payload)


class Provider:
    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def analyze(self, image_path, model, extra_instruction=""):
        index = min(self.calls, len(self.scores) - 1)
        self.calls += 1
        return VisionCallResult(
            scores=self.scores[index], tokens_in=10, tokens_out=5, latency_ms=1
        )


class Applier:
    def __init__(self):
        self.applied = []

    def snapshot(self):
        return WallpaperState(wallpaper="C:/old.jpg", style="22", tile="0")

    def apply(self, image, style="fill"):
        self.applied.append(str(image))
        return ApplyResult(ok=True)

    def restore(self, state):
        pass


def _scores(quality, formulaic):
    return VisionScores(
        quality=quality, creativity=70, novelty=60, formulaic=formulaic,
        nsfw=False, watermark=False, text_overlay="none",
        style_tags=["landscape"], mood_tags=["calm"], palette=["#112233"],
        subject="s", reason="r",
    )


def _candidates():
    return [
        Candidate(
            "wallhaven", str(index + 1), "t", "p", "f", f"https://img/{index}.png",
            "image", 3840, 2160, 1000, [], {},
        )
        for index in range(3)
    ]


def test_one_apply_per_day_for_a_week(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)

    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, SynthSession(), [DaySource()], vision, budget)
    service = ApplyService(conn, Applier(), clock=clock)
    machine = DailyStateMachine(conn, cfg, clock, pipeline, service)

    assert machine.tick().action == "applied"
    assert machine.tick().action == "already_changed"

    for _ in range(6):
        clock.advance(1)
        assert machine.tick().action == "applied"

    rows = conn.execute(
        "select change_date from change_log where success=1 order by change_date"
    ).fetchall()
    assert len(rows) == 7
    assert len({row["change_date"] for row in rows}) == 7


def _pipeline(tmp_path, session, provider, clock):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    vision = VisionClient(provider, conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, session, [Source(_candidates())], vision, budget)
    return cfg, conn, pipeline, budget


def test_one_failing_preview_does_not_abort_the_run(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession(broken=["https://img/0.png"])
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)

    pipeline.prepare(clock.now())

    assert len(pipeline.shortlist) == 2


def _on_disk(tmp_path, name="asset.jpg"):
    """A file the applier can use, so nothing is downloaded for the full size."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not inspected: the asset is already on disk")
    return path


def test_stale_dynamic_rows_do_not_starve_a_fresh_image(tmp_path):
    """The pool used to be dynamic + static, and the dynamic half never shrank.

    Every day's workshop backlog sat in front of the images, so a small
    download budget was spent on stale scenes and not one fresh image was ever
    scored -- the folder stopped growing while the log said the pool was big.
    """
    cfg = _config(tmp_path)
    cfg = replace(cfg, app=replace(cfg.app, max_downloads_per_day=2))
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)

    project = _on_disk(tmp_path, "scene/project.json")
    asset = _on_disk(tmp_path)
    scenes = [
        Candidate(
            "we_workshop", f"s{index}", "t", "p", str(project),
            f"https://img/scene{index}.png", "scene", 3840, 2160, 1000, [], {},
        )
        for index in range(4)
    ]
    fresh = [
        Candidate(
            "wallhaven", f"i{index}", "t", "p", str(asset),
            f"https://img/fresh{index}.png", "image", 3840, 2160, 1000, [], {},
        )
        for index in range(2)
    ]
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source(scenes + fresh)], vision, budget,
        power=_AcPower(),
    )

    pipeline.prepare(clock.now())

    assert pipeline.shortlist
    assert any(d.media_type == "image" for d in pipeline.shortlist)


def test_a_saved_preview_is_not_downloaded_again(tmp_path):
    """A second pass has nothing new to fetch, so the cap must not cut it short."""
    cfg = _config(tmp_path)
    cfg = replace(cfg, app=replace(cfg.app, max_downloads_per_day=1))
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    asset = _on_disk(tmp_path)
    candidates = [
        Candidate(
            "wallhaven", f"c{index}", "t", "p", str(asset),
            f"https://img/cached{index}.png", "image", 3840, 2160, 1000, [], {},
        )
        for index in range(3)
    ]
    session = SynthSession()
    pipeline = RealPipeline(cfg, conn, session, [Source(candidates)], vision, budget)

    pipeline.prepare(clock.now())
    assert len(pipeline.shortlist) == 1
    fetched = session.calls

    pipeline.prepare(clock.now(), force=True)

    # Exactly one new preview fetched; the one saved by the first pass is free.
    assert session.calls - fetched == 1
    assert len(pipeline.shortlist) == 2


class _DeadHostSession:
    """Every request times out, the way w.wallhaven.cc does without a proxy."""

    def __init__(self):
        self.calls = 0

    def get(self, url, stream=True, timeout=30):
        self.calls += 1
        raise RuntimeError("connect timeout")


def test_a_used_up_pool_still_offers_a_previously_used_wallpaper(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    # Remote candidates, so the full image is materialised on disk.
    candidates = [
        Candidate(
            "wallhaven", str(index + 1), "t", "p", f"https://img/{index}.png",
            f"https://img/{index}.png", "image", 3840, 2160, 1000, [], {},
        )
        for index in range(3)
    ]
    pipeline = RealPipeline(
        cfg, conn, BigSession(), [Source(candidates)], vision, budget
    )
    pipeline.prepare(clock.now())
    assert pipeline.shortlist
    assert any(path.is_file() for path in (Path(cfg.app.data_dir) / "wallpapers").glob("*.jpg"))
    for row in conn.execute("select id from wallpapers").fetchall():
        conn.execute(
            "insert into decisions(decision_date,wallpaper_id,explore_score,"
            "personalize_score,final_score,reason,status,created_at)"
            " values('2026-10-18',?,50,50,50,'r','applied','x')",
            (row["id"],),
        )
    conn.commit()

    # Everything is a duplicate now.
    pipeline.prepare(clock.now(), force=True)
    assert pipeline.pick(clock.now()) is None

    repeat = pipeline.repeat_pick(clock.now())

    assert repeat is not None
    assert repeat.apply_path is not None and repeat.apply_path.is_file()


def test_the_reuse_fallback_survives_a_vision_provider_switch(tmp_path):
    """The model name is part of the analysis cache key.

    Switching provider leaves almost no history scored under the new model,
    so requiring *today's* model here made the fallback return None for a pool
    the user had used for weeks. On 2026-10-27 that is exactly what happened:
    every one of the twenty reuse candidates had a ``glm-4.6v`` analysis and
    none had a ``deepseek-flash`` one, so two clicks were answered with 48 s
    and 53 s of "kept" and the wallpaper never changed.
    """
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    candidates = [
        Candidate(
            "wallhaven", str(index + 1), "t", "p", f"https://img/{index}.png",
            f"https://img/{index}.png", "image", 3840, 2160, 1000, [], {},
        )
        for index in range(3)
    ]
    pipeline = RealPipeline(
        cfg, conn, BigSession(), [Source(candidates)], vision, budget
    )
    pipeline.prepare(clock.now())
    assert pipeline.shortlist

    # Everything the previous provider scored: exactly what a switch leaves.
    conn.execute("update analyses set model='glm-4.6v'")
    for row in conn.execute("select id from wallpapers").fetchall():
        conn.execute(
            "insert into decisions(decision_date,wallpaper_id,explore_score,"
            "personalize_score,final_score,reason,status,created_at)"
            " values('2026-10-18',?,50,50,50,'r','applied','x')",
            (row["id"],),
        )
    conn.commit()

    pipeline.prepare(clock.now(), force=True)
    assert pipeline.pick(clock.now()) is None

    repeat = pipeline.repeat_pick(clock.now())

    assert repeat is not None, "the fallback went blind after the model changed"
    assert repeat.apply_path is not None and repeat.apply_path.is_file()


def test_a_previously_used_wallpaper_the_user_rated_low_is_not_reused(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source(_candidates())], vision, budget
    )
    pipeline.prepare(clock.now())
    for row in conn.execute("select id from wallpapers").fetchall():
        conn.execute(
            "insert into decisions(decision_date,wallpaper_id,explore_score,"
            "personalize_score,final_score,reason,status,created_at)"
            " values('2026-10-18',?,50,50,50,'r','applied','x')",
            (row["id"],),
        )
        conn.execute(
            "insert into ratings(wallpaper_id,rating,comment,derived_tags_json,"
            "created_at) values(?,1,'','[]','2026-10-18T10:00:00')",
            (row["id"],),
        )
    conn.commit()

    assert pipeline.repeat_pick(clock.now()) is None


def test_a_dead_download_host_is_tried_once_per_prepare(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    preview = Path(cfg.app.data_dir) / "cache" / "normalized" / "p.jpg"
    preview.parent.mkdir(parents=True)
    _checkerboard(8).save(preview, "JPEG")
    candidates = [
        Candidate(
            "wallhaven", f"d{index}", "t", "p",
            f"https://w.wallhaven.cc/full/xx/wallhaven-d{index}.jpg",
            str(preview), "image", 3840, 2160, 1000, [], {},
        )
        for index in range(3)
    ]
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = _DeadHostSession()
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, session, [Source(candidates)], vision, budget)

    pipeline.prepare(clock.now())

    # One timeout marks the host dead; the other two cost nothing.
    assert session.calls == 1


def test_cached_pool_needs_the_full_resolution_copy_and_keeps_urls(tmp_path):
    import json

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    data = Path(cfg.app.data_dir)
    screening = data / "cache" / "normalized" / "abc.jpg"
    screening.parent.mkdir(parents=True)
    _checkerboard(8).save(screening, "JPEG")
    conn.execute(
        "insert into wallpapers(source,source_id,title,file_url,media_type,width,height,"
        "local_path,created_at) values('wallhaven','old','旧图',"
        "'https://w.wallhaven.cc/full/xx/wallhaven-old.png','image',3840,2160,?,'x')",
        (str(screening),),
    )
    wid = conn.execute("select max(id) from wallpapers").fetchone()[0]
    scores = _scores(90, 10)
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,nsfw,style_tags_json,mood_tags_json,"
        "palette_json,tokens_in,tokens_out,cost_cny,created_at)"
        " values(?,'v','h',?,90,70,60,10,0,'[]','[]','[]',1,1,0,'x')",
        (MODEL, json.dumps(scores.__dict__)),
    )
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
        " select ?, max(id), 'x' from analyses",
        (wid,),
    )
    conn.commit()
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, SynthSession(), [Source([])], vision, budget)

    # Without a full-resolution copy the remote URL is kept, so the
    # materialiser fetches it; the screening copy only serves as preview.
    # Never the other way round: a 1024px image on the desktop looked blurry.
    candidates = pipeline._cached_local_candidates({"image"}, 60)

    assert len(candidates) == 1
    assert candidates[0].file_url.startswith("https://")
    assert candidates[0].preview_url == str(screening)


def test_a_stale_analysis_is_replaced_when_the_provider_answers(tmp_path):
    import json


    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    data = Path(cfg.app.data_dir)
    image = data / "cache" / "normalized" / "abc.jpg"
    image.parent.mkdir(parents=True)
    _checkerboard(8).save(image, "JPEG")
    full = data / "wallpapers" / "1.jpg"
    full.parent.mkdir(parents=True)
    _checkerboard(8).save(full, "JPEG", quality=95)
    conn.execute(
        "insert into wallpapers(source,source_id,title,media_type,width,height,"
        "local_path,created_at) values('wallhaven','old','旧图','image',3840,2160,?,'x')",
        (str(image),),
    )
    wid = conn.execute("select max(id) from wallpapers").fetchone()[0]
    full = data / "wallpapers" / f"{wid}.jpg"
    _checkerboard(8).save(full, "JPEG", quality=95)
    old = _scores(40, 90)  # a v1 "sugar water" verdict: pretty, formulaic
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,nsfw,style_tags_json,mood_tags_json,"
        "palette_json,tokens_in,tokens_out,cost_cny,created_at)"
        " values(?,'v1','h',?,40,40,30,90,0,'[]','[]','[]',1,1,0,'x')",
        (MODEL, json.dumps(old.__dict__)),
    )
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
        " select ?, max(id), 'x' from analyses",
        (wid,),
    )
    conn.commit()
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    provider = Provider([_scores(90, 10)])
    vision = VisionClient(provider, conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, SynthSession(), [Source([])], vision, budget)

    pipeline.prepare(clock.now())

    # The stored v1 verdict was not trusted; a fresh call was made.
    assert provider.calls == 1
    assert pipeline.shortlist
    latest = conn.execute("select max(id) from analyses").fetchone()[0]
    version = conn.execute(
        "select prompt_version from analyses where id=?", (latest,)
    ).fetchone()[0]
    assert version == PROMPT_VERSION


def test_a_cached_row_without_any_full_resolution_source_is_skipped(tmp_path):

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    data = Path(cfg.app.data_dir)
    screening = data / "cache" / "normalized" / "abc.jpg"
    screening.parent.mkdir(parents=True)
    _checkerboard(8).save(screening, "JPEG")
    conn.execute(
        "insert into wallpapers(source,source_id,title,file_url,media_type,width,height,"
        "local_path,created_at) values('bing','old','旧图','','image',3840,2160,?,'x')",
        (str(screening),),
    )
    wid = conn.execute("select max(id) from wallpapers").fetchone()[0]
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,nsfw,style_tags_json,mood_tags_json,"
        "palette_json,tokens_in,tokens_out,cost_cny,created_at)"
        " values(?,'v','h','{}',90,70,60,10,0,'[]','[]','[]',1,1,0,'x')",
        (MODEL,),
    )
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
        " select ?, max(id), 'x' from analyses",
        (wid,),
    )
    conn.commit()
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, SynthSession(), [Source([])], vision, budget)

    assert pipeline._cached_local_candidates({"image"}, 60) == []


def test_a_wallpaper_rated_one_never_comes_back(tmp_path):
    import json

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    data = Path(cfg.app.data_dir)
    scores = _scores(90, 10)
    disliked = liked = None
    for index, rating in enumerate((1, 8)):
        screening = data / "cache" / "normalized" / f"p{index}.jpg"
        screening.parent.mkdir(parents=True, exist_ok=True)
        _checkerboard(8).save(screening, "JPEG")
        conn.execute(
            "insert into wallpapers(source,source_id,title,media_type,width,height,"
            "local_path,created_at) values('wallhaven',?, 't','image',3840,2160,?,'x')",
            (f"p{index}", str(screening)),
        )
        wid = conn.execute("select max(id) from wallpapers").fetchone()[0]
        full = data / "wallpapers" / f"{wid}.jpg"
        full.parent.mkdir(parents=True, exist_ok=True)
        _checkerboard(8).save(full, "JPEG", quality=95)
        conn.execute(
            "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
            "creativity,novelty,formulaic,nsfw,style_tags_json,mood_tags_json,"
            "palette_json,tokens_in,tokens_out,cost_cny,created_at)"
            " values(?,?,?,?,90,70,60,10,0,'[]','[]','[]',1,1,0,'x')",
            (MODEL, PROMPT_VERSION, f"h{index}", json.dumps(scores.__dict__)),
        )
        conn.execute(
            "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
            " select ?, max(id), 'x' from analyses",
            (wid,),
        )
        conn.execute(
            "insert into ratings(wallpaper_id,rating,comment,derived_tags_json,"
            "created_at) values(?,?,'','[]','2026-10-18T10:00:00')",
            (wid, rating),
        )
        if rating == 1:
            disliked = wid
        else:
            liked = wid
    conn.commit()
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, SynthSession(), [Source([])], vision, budget)

    pipeline.prepare(clock.now())

    ids = [decision.wallpaper_id for decision in pipeline.shortlist]
    assert liked in ids
    assert disliked not in ids


def test_profile_learns_from_the_analysis_of_the_rated_wallpaper(tmp_path):
    import json

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,title,created_at)"
        " values('wallhaven','p1','雾','x')"
    )
    wid = conn.execute("select max(id) from wallpapers").fetchone()[0]
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,nsfw,style_tags_json,mood_tags_json,"
        "palette_json,tokens_in,tokens_out,cost_cny,created_at)"
        " values(?,'v2','h','{}',90,80,80,10,0,?,'[\"calm\"]','[]',1,1,0,'x')",
        (MODEL, json.dumps(["fog", "muted-palette"])),
    )
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
        " select ?, max(id), 'x' from analyses",
        (wid,),
    )
    conn.execute(
        "insert into ratings(wallpaper_id,rating,comment,derived_tags_json,created_at)"
        " values(?,10,'','[\"landscape\"]','2026-10-17T10:00:00')",
        (wid,),
    )
    conn.commit()
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, SynthSession(), [], vision, budget)

    profile = pipeline._profile(clock.now())

    assert profile.tag_affinity(["fog"]) > 50
    assert profile.tag_affinity(["calm"]) > 50


def test_thin_pool_falls_back_to_downloaded_wallpapers(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    image = Path(cfg.app.data_dir) / "cache" / "normalized" / "abc.jpg"
    image.parent.mkdir(parents=True)
    _checkerboard(8).save(image, "JPEG")
    conn.execute(
        "insert into wallpapers(source,source_id,title,media_type,width,height,"
        "local_path,created_at) values('wallhaven','old','旧图','image',3840,2160,?,'x')",
        (str(image),),
    )
    wid = conn.execute("select max(id) from wallpapers").fetchone()[0]
    full = Path(cfg.app.data_dir) / "wallpapers" / f"{wid}.jpg"
    full.parent.mkdir(parents=True)
    _checkerboard(8).save(full, "JPEG", quality=95)
    scores = _scores(90, 10)
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,nsfw,style_tags_json,mood_tags_json,"
        "palette_json,tokens_in,tokens_out,cost_cny,created_at)"
        " values(?,?,'h',?,90,70,60,10,0,'[]','[]','[]',1,1,0,'x')",
        (MODEL, PROMPT_VERSION, __import__("json").dumps(scores.__dict__)),
    )
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
        " select ?, max(id), 'x' from analyses",
        (wid,),
    )
    conn.commit()
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = SynthSession()
    provider = Provider([_scores(90, 10)])
    vision = VisionClient(provider, conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, session, [Source([])], vision, budget)

    pipeline.prepare(clock.now())

    # Nothing was fetched and nothing was re-scored: both were on disk.
    assert session.calls == 0
    assert provider.calls == 0
    assert len(pipeline.shortlist) == 1


def test_repeated_rate_limits_end_the_pass_early(tmp_path):
    from dailywallpaper.app import RATE_LIMIT_ABORT_AFTER

    clock = Clock(datetime(2026, 10, 18, 9, 0))
    provider = RateLimitedProvider()
    candidates = [
        Candidate(
            "wallhaven", f"r{index}", "t", "p", "f", f"https://img/r{index}.png",
            "image", 3840, 2160, 1000, [], {},
        )
        for index in range(10)
    ]
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    vision = VisionClient(provider, conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source(candidates)], vision, budget
    )

    pipeline.prepare(clock.now())

    # Without the breaker every one of the ten candidates paid its own 429.
    assert provider.calls == RATE_LIMIT_ABORT_AFTER


def test_prepare_runs_once_per_day(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)

    pipeline.prepare(clock.now())
    first = session.calls
    pipeline.prepare(clock.now())

    assert session.calls == first


def test_prepare_warns_once_when_the_budget_is_nearly_spent(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)
    conn.execute(
        "insert into api_usage(ts,provider,model,purpose,cost_cny,status_code)"
        " values('2026-10-01T08:00:00','zhipu','m','screen',8.5,200)"
    )
    conn.commit()

    pipeline.prepare(clock.now())

    rows = conn.execute("select count(*) from state where key like 'budget_warned_%'").fetchone()
    assert rows[0] == 1


def test_prepare_stores_a_phash_for_accepted_candidates(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)

    pipeline.prepare(clock.now())

    hashes = [r[0] for r in conn.execute("select phash from wallpapers").fetchall()]
    assert len(hashes) == 3
    assert all(hashes)


def test_pick_records_a_decision_row(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)
    pipeline.prepare(clock.now())

    decision = pipeline.pick(clock.now())

    assert decision is not None
    row = conn.execute("select * from decisions").fetchone()
    assert row["wallpaper_id"] == decision.wallpaper_id
    # The status only becomes 'applied' after the desktop actually changed.
    assert row["status"] == "shortlisted"
    pipeline.mark_applied(decision)
    row = conn.execute("select * from decisions").fetchone()
    assert row["status"] == "applied"
    assert row["decision_date"] == "2026-10-18"


def test_low_quality_candidates_are_gated_out(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)

    clock = Clock(datetime(2026, 10, 18, 9, 0))
    provider = Provider([_scores(50, 20)] * 3)
    vision = VisionClient(provider, conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, ImageSession(), [Source(_candidates())], vision, budget)
    machine = DailyStateMachine(conn, cfg, clock, pipeline, ApplyService(conn, Applier()))

    assert machine.tick().action == "kept"
    assert conn.execute("select count(*) from change_log").fetchone()[0] == 0


class ModelProvider:
    """Returns a different score per model so the review is observable."""

    def __init__(self, by_model):
        self.by_model = by_model
        self.calls = []

    def analyze(self, image_path, model, extra_instruction=""):
        self.calls.append(model)
        return VisionCallResult(
            scores=self.by_model[model], tokens_in=10, tokens_out=5, latency_ms=1
        )


class CountingBudget:
    def __init__(self, allowed):
        self.allowed = allowed
        self.checks = 0

    def check(self, estimate):
        self.checks += 1
        return self.checks <= self.allowed

    def warn_if_needed(self):
        return False


def _review_pipeline(tmp_path, review_top_n=5, allowed=100):
    cfg = _config(tmp_path, review_top_n=review_top_n)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    provider = ModelProvider({"glm-4.6v-flash": _scores(70, 20), "review-model": _scores(91, 5)})
    vision = VisionClient(provider, conn, cfg.vision.pricing)
    budget = CountingBudget(allowed)
    pipeline = RealPipeline(
        cfg, conn, ImageSession(), [Source(_candidates())], vision, budget
    )
    return conn, pipeline, provider


def test_review_replaces_the_screen_scores(tmp_path):
    conn, pipeline, provider = _review_pipeline(tmp_path)
    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert pipeline.shortlist[0].reviewed is True
    assert pipeline.shortlist[0].scores.quality == 91
    assert "review-model" in provider.calls


def test_review_stops_when_the_budget_is_gone(tmp_path):
    # 3 screen checks pass, then the budget runs out before the reviews.
    conn, pipeline, provider = _review_pipeline(tmp_path, allowed=4)
    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert sum(1 for decision in pipeline.shortlist if decision.reviewed) == 1


def test_review_top_n_zero_skips_review(tmp_path):
    conn, pipeline, provider = _review_pipeline(tmp_path, review_top_n=0)
    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert all(not decision.reviewed for decision in pipeline.shortlist)
    assert "review-model" not in provider.calls


def test_build_pipeline_requires_pricing_for_every_model(tmp_path):
    from dataclasses import replace

    import pytest

    from dailywallpaper.app import build_pipeline
    from dailywallpaper.config import VisionPricing

    cfg = _config(tmp_path)
    broken = replace(
        cfg,
        vision=replace(
            cfg.vision, pricing={"glm-4.6v-flash": VisionPricing(1.0, 2.0)}
        ),
    )
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)

    with pytest.raises(ValueError, match="review-model"):
        build_pipeline(broken, conn)


def _score(creativity, novelty=60, quality=90, formulaic=10):
    return VisionScores(
        quality=quality, creativity=creativity, novelty=novelty,
        formulaic=formulaic, nsfw=False, watermark=False, text_overlay="none",
        style_tags=["landscape"], mood_tags=["calm"], palette=["#112233"],
        subject="s", reason="r",
    )


def test_a_malformed_analysis_is_not_retried_on_the_next_prepare(tmp_path):
    # One image whose palette the model keeps returning malformed burned a
    # vision call on every prepare. It is remembered as a rejection instead.
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    still = Candidate("bing", "i1", "图片", "", "f", "https://img/day1/1.png",
                      "image", 3840, 2160, 0, [], {})

    class Broken:
        def __init__(self):
            self.calls = 0

        def analyze(self, image_path, model, extra_instruction=""):
            self.calls += 1
            raise VisionError("bad palette")

    broken = Broken()
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source([still])],
        VisionClient(broken, conn, cfg.vision.pricing),
        CountingBudget(100), power=_AcPower(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    rows = conn.execute("select reason from rejections").fetchall()
    assert rows and "bad palette" in rows[0][0]
    first_calls = broken.calls

    pipeline.shortlist = []
    pipeline._prepared_on = None
    pipeline.prepare(datetime(2026, 10, 18, 10, 0), force=True)

    assert broken.calls == first_calls, "the known-bad image was tried again"


def test_every_candidate_competes_in_one_pool(tmp_path):
    # There is no static day / dynamic day any more and no comparing one
    # pool's best against the other's: everything is scored and ranked
    # together, and anything above the floor stays on the shortlist.
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    asset = tmp_path / "scene.pkg"
    asset.write_bytes(b"x")
    scene = Candidate("we_workshop", "s1", "场景", "", str(asset),
                      "https://img/day1/0.png", "scene", 0, 0, 0, [], {})
    still = Candidate("bing", "i1", "图片", "", "f", "https://img/day1/1.png",
                      "image", 3840, 2160, 0, [], {})
    # Live candidates are listed first in the pool, so they are scored first.
    vision = VisionClient(Provider([_score(90), _score(70)]), conn, cfg.vision.pricing)
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source([scene, still])], vision,
        CountingBudget(100), power=_AcPower(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert [decision.media_type for decision in pipeline.shortlist] == ["scene", "image"]
    assert pipeline.shortlist[0].calibrated is False


def test_a_lower_scoring_candidate_still_ranks_second(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    asset = tmp_path / "scene.pkg"
    asset.write_bytes(b"x")
    scene = Candidate("we_workshop", "s1", "场景", "", str(asset),
                      "https://img/day1/0.png", "scene", 0, 0, 0, [], {})
    still = Candidate("bing", "i1", "图片", "", "f", "https://img/day1/1.png",
                      "image", 3840, 2160, 0, [], {})
    vision = VisionClient(Provider([_score(60), _score(90)]), conn, cfg.vision.pricing)
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source([scene, still])], vision,
        CountingBudget(100), power=_AcPower(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert [decision.media_type for decision in pipeline.shortlist] == ["image", "scene"]


def test_mixed_ignores_a_dynamic_candidate_that_is_not_ready(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    root = tmp_path / "steam_workshop"
    remote = Candidate(
        "steam_workshop", "123", "Neon", "", str(root / "123" / "project.json"),
        "https://img/day1/0.png", "scene", 0, 0, 0, [], {},
    )
    still = Candidate("bing", "i1", "图片", "", "f", "https://img/day1/1.png",
                      "image", 3840, 2160, 0, [], {})
    vision = VisionClient(Provider([_score(95), _score(50)]), conn, cfg.vision.pricing)
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source([remote, still])], vision,
        CountingBudget(100), power=_AcPower(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert [decision.media_type for decision in pipeline.shortlist] == ["image"]


def test_mixed_does_not_evaluate_dynamic_on_battery(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    asset = tmp_path / "scene.pkg"
    asset.write_bytes(b"x")
    scene = Candidate("we_workshop", "s1", "场景", "", str(asset),
                      "https://img/day1/0.png", "scene", 0, 0, 0, [], {})
    still = Candidate("bing", "i1", "图片", "", "f", "https://img/day1/1.png",
                      "image", 3840, 2160, 0, [], {})
    provider = Provider([_score(99), _score(10)])

    class _Battery:
        def on_battery(self):
            return True

    vision = VisionClient(provider, conn, cfg.vision.pricing)
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source([scene, still])], vision,
        CountingBudget(100), power=_Battery(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert provider.calls == 1
    assert [decision.media_type for decision in pipeline.shortlist] == ["image"]


def test_cached_dynamic_scores_do_not_eat_the_refill_budget(tmp_path):
    # A refill may spend a bounded number of FRESH analyses. Cache hits cost
    # nothing, so eight cached dynamic candidates must not leave the static
    # pool unscored -- that would hand the day to dynamic by default.
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    scenes = []
    for index in range(8):
        asset = tmp_path / f"scene{index}.pkg"
        asset.write_bytes(b"x")
        scenes.append(Candidate(
            "we_workshop", f"s{index}", "场景", "", str(asset),
            f"https://img/s{index}.png", "scene", 0, 0, 0, [], {},
        ))
    stills = [
        Candidate("bing", f"i{index}", "图片", "", "f",
                  f"https://img/i{index}.png", "image", 3840, 2160, 0, [], {})
        for index in range(3)
    ]
    dynamic_cfg = replace(
        cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only")
    )
    first = RealPipeline(
        dynamic_cfg, conn, SynthSession(), [Source(scenes)],
        VisionClient(Provider([_score(60)]), conn, cfg.vision.pricing),
        CountingBudget(100), power=_AcPower(),
    )
    first.prepare(datetime(2026, 10, 18, 9, 0))
    assert len(first.shortlist) == 8

    second = RealPipeline(
        cfg, conn, SynthSession(), [Source(scenes + stills)],
        VisionClient(Provider([_score(95)]), conn, cfg.vision.pricing),
        CountingBudget(100), power=_AcPower(),
    )
    second._reanalyze_budget = 8
    second.prepare(datetime(2026, 10, 18, 9, 0), force=True)

    assert [decision.media_type for decision in second.shortlist][:1] == ["image"]


def test_a_static_day_falls_back_when_only_dynamic_exists(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    asset = tmp_path / "scene.pkg"
    asset.write_bytes(b"x")
    scene = Candidate("we_workshop", "s1", "场景", "", str(asset),
                      "https://img/day1/0.png", "scene", 0, 0, 0, [], {})
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="static_only"))
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source([scene])], vision, CountingBudget(100),
        power=_AcPower(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert len(pipeline.shortlist) == 1


def test_a_dry_run_does_not_consume_the_day(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)
    service = ApplyService(conn, Applier(), clock=clock)
    machine = DailyStateMachine(conn, cfg, clock, pipeline, service)

    machine.tick(dry_run=True)

    row = conn.execute(
        "select 1 from state where key='prepared_2026-10-18'"
    ).fetchone()
    assert row is None


def test_a_lost_shortlist_can_be_rebuilt_the_same_day(tmp_path):
    # A complete prepare is persisted, so a restart does not re-pay for the
    # pool; if that marker is missing (an older run, a partial pass) the day
    # can still be rebuilt.
    clock = Clock(datetime(2026, 10, 18, 7, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)

    pipeline.prepare(clock.now())
    assert conn.execute(
        "select 1 from state where key='prepared_2026-10-18'"
    ).fetchone() is not None

    conn.execute("delete from state where key='prepared_2026-10-18'")
    conn.commit()
    fresh = RealPipeline(cfg, conn, session, [Source(_candidates())],
                         VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing),
                         budget)
    fresh.prepare(clock.now())
    assert fresh.pick(clock.now()) is not None


def test_a_dynamic_day_falls_back_when_every_scene_fails_prefilter(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    scene = Candidate("we_workshop", "s1", "场景", "", "https://img/blur.png",
                      "https://img/blur.png", "scene", 0, 0, 0, [], {})
    still = Candidate("bing", "i1", "图片", "", "f", "https://img/day1/1.png",
                      "image", 3840, 2160, 0, [], {})

    class BlurSession(SynthSession):
        def get(self, url, stream=True, timeout=30):
            if "blur" in url:
                buffer = io.BytesIO()
                Image.new("RGB", (320, 512), (128, 128, 128)).save(buffer, "PNG")
                return _Response(buffer.getvalue())
            return super().get(url, stream=stream, timeout=timeout)

    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only"))
    pipeline = RealPipeline(
        cfg, conn, BlurSession(), [Source([scene, still])], vision, CountingBudget(100),
        power=_AcPower(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert len(pipeline.shortlist) == 1
    assert pipeline.shortlist[0].scores is not None


def test_a_video_candidate_is_scored_from_a_contact_sheet(tmp_path):
    from dataclasses import replace
    import dailywallpaper.pipeline.evaluate as evaluate_module

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"fake")
    video = Candidate("we_workshop", "v1", "视频", "", str(video_file),
                      str(video_file), "video", 0, 0, 0, [], {})

    calls = []
    original = evaluate_module.video_contact_sheet

    def fake_sheet(asset, dest, ffmpeg="ffmpeg"):
        calls.append(Path(asset))
        dest.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        _checkerboard(8).save(buffer, "PNG")
        Path(dest).write_bytes(buffer.getvalue())
        return Path(dest)

    evaluate_module.video_contact_sheet = fake_sheet
    try:
        vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
        cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only"))
        pipeline = RealPipeline(
            cfg, conn, SynthSession(), [Source([video])], vision, CountingBudget(100),
            power=_AcPower(),
        )
        # CI runners have no ffmpeg; force the video branch so the test checks
        # the contact-sheet path rather than the environment.
        pipeline._ffmpeg_ok = True
        pipeline.prepare(datetime(2026, 10, 18, 9, 0))
    finally:
        evaluate_module.video_contact_sheet = original

    assert calls == [video_file]
    assert len(pipeline.shortlist) == 1
    # A contact sheet is the shape the calibration was measured on.
    assert pipeline.shortlist[0].calibrated is True
    assert pipeline.shortlist[0].media_type == "video"


def test_battery_never_falls_back_into_a_dynamic_wallpaper(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    scene = Candidate("we_workshop", "s1", "场景", "", "https://img/day1/0.png",
                      "https://img/day1/0.png", "scene", 0, 0, 0, [], {})

    class _Battery:
        def on_battery(self):
            return True

    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="static_only"))
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source([scene])], vision, CountingBudget(100),
        power=_Battery(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert pipeline.shortlist == []


def test_video_falls_back_to_the_poster_without_ffmpeg(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"fake")
    video = Candidate("we_workshop", "v1", "视频", "", str(video_file),
                      "https://img/day1/1.png", "video", 0, 0, 0, [], {})
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only"))
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source([video])], vision, CountingBudget(100),
        power=_AcPower(),
    )
    pipeline._ffmpeg_ok = False

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert len(pipeline.shortlist) == 1


def test_the_workshop_source_is_off_unless_enabled(tmp_path, monkeypatch):
    from dataclasses import replace

    import keyring

    from dailywallpaper.app import build_pipeline

    # A CI runner has no credential store; the pipeline refuses to build
    # without a model key, so provide one for this wiring test.
    monkeypatch.setattr(keyring, "get_password", lambda *args, **kwargs: "fake-key")
    cfg = _config(tmp_path)
    cfg = replace(
        cfg,
        sources=replace(cfg.sources, we_workshop_enabled=False),
        wallpaper_engine=replace(
            cfg.wallpaper_engine,
            install_dir=str(Path(r"D:/games/steam/steamapps/common/wallpaper_engine")),
        ),
    )
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)

    names = [type(s).__name__ for s in build_pipeline(cfg, conn).sources]
    assert "WorkshopSource" not in names


def test_a_dry_run_records_a_shortlist_not_an_apply(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    cfg, conn, pipeline, budget = _pipeline(tmp_path, ImageSession(), Provider([_scores(90, 10)]), clock)
    machine = DailyStateMachine(conn, cfg, clock, pipeline, ApplyService(conn, Applier(), clock=clock))

    machine.tick(dry_run=True)

    statuses = [row["status"] for row in conn.execute("select status from decisions")]
    assert statuses == ["shortlisted"]


def test_no_download_is_wasted_once_the_budget_is_gone(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    scenes = [
        Candidate("we_workshop", "s1", "场景", "", "f",
                  "https://img/0.png", "scene", 0, 0, 0, [], {}),
        Candidate("we_workshop", "s2", "场景", "", "f",
                  "https://img/1.png", "scene", 0, 0, 0, [], {}),
    ]
    still = Candidate("bing", "i1", "图片", "", "f",
                      "https://img/2.png", "image", 3840, 2160, 0, [], {})
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only"))
    vision = VisionClient(Provider([_scores(10, 90)]), conn, cfg.vision.pricing)
    session = ImageSession()

    class _Ac:
        def on_battery(self):
            return False

    # Two screen calls are allowed; both scenes are gated out, so the static
    # fallback would download a third preview before noticing the budget is gone.
    pipeline = RealPipeline(
        cfg, conn, session, [Source(scenes + [still])], vision,
        CountingBudget(2), power=_Ac(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert pipeline.shortlist == []
    assert session.calls == 2


def test_a_remote_workshop_item_waits_for_its_download(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    root = tmp_path / "steam_workshop"
    remote = Candidate(
        "steam_workshop", "123", "Neon", "", str(root / "123" / "project.json"),
        "https://img/0.png", "scene", 0, 0, 0, [], {},
    )
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only"))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)

    class _Ac:
        def on_battery(self):
            return False

    first = RealPipeline(
        cfg, conn, ImageSession(), [Source([remote])], vision,
        CountingBudget(100), power=_Ac(),
    )
    first.prepare(datetime(2026, 10, 18, 9, 0))
    assert first.shortlist == []

    (root / "123").mkdir(parents=True)
    (root / "123" / "project.json").write_text("{}", encoding="utf-8")
    second = RealPipeline(
        cfg, conn, ImageSession(), [Source([remote])], vision,
        CountingBudget(100), power=_Ac(),
    )
    second.prepare(datetime(2026, 10, 18, 9, 0))

    assert len(second.shortlist) == 1
    assert second.shortlist[0].ready is True
    assert second.shortlist[0].apply_path == root / "123" / "project.json"


def test_an_unready_remote_item_does_not_block_the_fallback(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    root = tmp_path / "steam_workshop"
    remote = Candidate(
        "steam_workshop", "123", "Neon", "", str(root / "123" / "project.json"),
        "https://img/0.png", "scene", 0, 0, 0, [], {},
    )
    still = Candidate("bing", "i1", "图片", "", "f", "https://img/1.png",
                      "image", 3840, 2160, 0, [], {})
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only"))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)

    class _Ac:
        def on_battery(self):
            return False

    pipeline = RealPipeline(
        cfg, conn, ImageSession(), [Source([remote, still])], vision,
        CountingBudget(100), power=_Ac(),
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert [d.source for d in pipeline.shortlist] == ["bing"]


def test_a_steam_item_evicted_between_prepare_and_pick_is_skipped(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    root = tmp_path / "steam_workshop"
    (root / "123").mkdir(parents=True)
    (root / "123" / "project.json").write_text("{}", encoding="utf-8")
    remote = Candidate(
        "steam_workshop", "123", "Neon", "", str(root / "123" / "project.json"),
        "https://img/0.png", "scene", 0, 0, 0, [], {},
    )
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only"))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)

    class _Ac:
        def on_battery(self):
            return False

    pipeline = RealPipeline(
        cfg, conn, ImageSession(), [Source([remote])], vision,
        CountingBudget(100), power=_Ac(),
    )
    pipeline.prepare(datetime(2026, 10, 18, 9, 0))
    assert len(pipeline.shortlist) == 1

    (root / "123" / "project.json").unlink()

    assert pipeline.pick(datetime(2026, 10, 18, 9, 0)) is None


def _two_size_session():
    full = io.BytesIO()
    _checkerboard(24).resize((2560, 1440)).save(full, "PNG")
    small = io.BytesIO()
    _checkerboard(6).resize((320, 180)).save(small, "PNG")
    items = {
        "https://img/full.png": full.getvalue(),
        "https://img/preview.png": small.getvalue(),
    }

    class _S:
        def __init__(self):
            self.calls = 0

        def get(self, url, stream=True, timeout=30, **kwargs):
            self.calls += 1
            if url not in items:
                raise RuntimeError(f"404 {url}")
            return _Response(items[url])

    return _S()


def test_the_applied_file_is_the_full_resolution_image(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    candidate = Candidate(
        "wallhaven", "w1", "T", "", "https://img/full.png",
        "https://img/preview.png", "image", 2560, 1440, 0, [], {},
    )
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    pipeline = RealPipeline(
        cfg, conn, _two_size_session(), [Source([candidate])], vision, CountingBudget(100)
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    decision = pipeline.shortlist[0]
    apply_path = Path(decision.apply_path)
    assert apply_path.name.endswith(".jpg")
    assert apply_path.parent.name == "wallpapers"
    assert Image.open(apply_path).size == (2560, 1440)
    # The analysis copy stays small on purpose.
    assert Image.open(decision.image_path).size[0] <= 1024


def test_a_candidate_without_a_full_image_is_not_applied(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    candidate = Candidate(
        "wallhaven", "w1", "T", "", "https://img/missing.png",
        "https://img/preview.png", "image", 2560, 1440, 0, [], {},
    )
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    pipeline = RealPipeline(
        cfg, conn, _two_size_session(), [Source([candidate])], vision, CountingBudget(100)
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert pipeline.shortlist == []


def test_a_candidate_rejected_by_the_paid_review_is_dropped(tmp_path):
    from dataclasses import replace as dreplace

    cfg = _config(tmp_path, review_top_n=5)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    nsfw_review = dreplace(_scores(91, 5), nsfw=True)
    provider = ModelProvider({
        "glm-4.6v-flash": _scores(90, 10),
        "review-model": nsfw_review,
    })
    vision = VisionClient(provider, conn, cfg.vision.pricing)
    pipeline = RealPipeline(
        cfg, conn, ImageSession(), [Source(_candidates())], vision, CountingBudget(100)
    )

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    assert pipeline.shortlist == []


def test_prepare_stops_when_asked_so_a_manual_click_is_not_stuck(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, ImageSession(), provider, clock)
    pipeline.should_stop = lambda: provider.calls >= 1

    pipeline.prepare(clock.now())

    assert provider.calls == 1


def test_a_forced_prepare_retries_a_source_marked_down(tmp_path):
    from dailywallpaper.sources.repository import mark_source_down

    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)
    mark_source_down(conn, "Source", "2026-10-18")

    pipeline.prepare(clock.now())
    assert pipeline.shortlist == []

    pipeline.prepare(clock.now(), force=True)

    assert pipeline.shortlist


def test_downloaded_steam_items_survive_a_dead_workshop_api(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    project = tmp_path / "steam_workshop" / "123" / "project.json"
    project.parent.mkdir(parents=True)
    project.write_text("{}", encoding="utf-8")
    conn.execute(
        "insert into wallpapers(source,source_id,title,page_url,file_url,preview_url,"
        "media_type,width,height,file_size,tags_json,raw_json,created_at)"
        " values('steam_workshop','123','t','p',?,'https://img/cached.png',"
        "'video',0,0,0,'[]','{}','x')",
        (str(project),),
    )
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [], vision, budget, power=_AcPower()
    )

    pipeline.prepare(clock.now())

    assert any(decision.wallpaper_id == wid for decision in pipeline.shortlist)


def test_a_one_star_rating_drops_an_already_prepared_pick(tmp_path):
    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(
        cfg, conn, SynthSession(), [Source(_candidates())], vision, budget
    )
    pipeline.prepare(clock.now())
    top = pipeline.shortlist[0].wallpaper_id
    conn.execute(
        "insert into ratings(wallpaper_id,rating,comment,derived_tags_json,"
        "created_at) values(?,1,'糖水','[]','2026-10-18T10:00:00')",
        (top,),
    )
    conn.commit()

    picked = pipeline.pick(clock.now())

    assert picked is not None
    assert picked.wallpaper_id != top


class _BrokenSource:
    """A source that raises the way a blocked host does at login."""

    def __init__(self):
        self.calls = 0

    def fetch(self, limit=None):
        self.calls += 1
        raise RuntimeError("proxy not listening yet")


def test_a_source_failure_schedules_a_retry_instead_of_locking_the_day(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)
    broken = _BrokenSource()
    pipeline.sources = [Source(_candidates()), broken]

    pipeline.prepare(clock.now())

    assert pipeline.shortlist  # the working source still filled the pool
    assert conn.execute(
        "select 1 from state where key='prepared_2026-10-18'"
    ).fetchone() is None
    assert conn.execute(
        "select 1 from state where key='prepare_retry_at_2026-10-18'"
    ).fetchone() is not None

    # Once the retry moment passes, the next prepare runs again and, with
    # every source healthy, marks the day complete.
    conn.execute(
        "update state set value='0' where key='prepare_retry_at_2026-10-18'"
    )
    pipeline.sources = [Source(_candidates())]
    conn.execute("delete from state where key='source_down_Source_2026-10-18'")
    conn.commit()
    pipeline.prepare(clock.now())

    assert conn.execute(
        "select 1 from state where key='prepared_2026-10-18'"
    ).fetchone() is not None


def test_a_source_down_marker_without_a_timestamp_is_ignored(tmp_path):
    from dailywallpaper.sources.repository import source_is_down

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into state(key,value) values('source_down_WorkshopSource_2026-10-18','1')"
    )
    conn.commit()

    assert source_is_down(conn, "WorkshopSource", "2026-10-18") is False


def test_a_rejected_file_is_remembered_and_not_downloaded_again(tmp_path):
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    cfg, conn, pipeline, budget = _pipeline(
        tmp_path, ImageSession(), Provider([_scores(90, 10)]), clock
    )
    wid = _candidates()[0]
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wallpaper_id = conn.execute("select id from wallpapers").fetchone()[0]

    assert pipeline._rejected_recently(wallpaper_id) is False
    pipeline._record_rejection(wallpaper_id, ("blurry",))
    assert pipeline._rejected_recently(wallpaper_id) is True

    # An old verdict lapses so a fixed threshold can be retried.
    conn.execute("update rejections set created_at='2020-01-01T00:00:00'")
    conn.commit()
    assert pipeline._rejected_recently(wallpaper_id) is False


def test_a_dry_run_before_the_window_does_not_consume_the_day(tmp_path):
    # The pre-08:30 branch used to call prepare without forwarding dry_run, so
    # a morning rehearsal marked the day as prepared and the real tick then
    # found nothing to do.
    clock = Clock(datetime(2026, 10, 18, 7, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)
    service = ApplyService(conn, Applier(), clock=clock)
    machine = DailyStateMachine(conn, cfg, clock, pipeline, service)

    assert machine.tick(dry_run=True).action == "background"

    assert conn.execute(
        "select 1 from state where key='prepared_2026-10-18'"
    ).fetchone() is None


def test_a_pending_retry_still_fires_after_the_day_has_changed(tmp_path):
    # A prepare that ran before the proxy came up schedules a rebuild in 30
    # minutes. Nothing else calls prepare once the day is marked as changed, so
    # a machine that boots after 08:30 used to keep the degraded pool forever.
    clock = Clock(datetime(2026, 10, 18, 9, 0))
    session = ImageSession()
    provider = Provider([_scores(90, 10)])
    cfg, conn, pipeline, budget = _pipeline(tmp_path, session, provider, clock)
    pipeline.sources = [Source(_candidates()), _BrokenSource()]
    service = ApplyService(conn, Applier(), clock=clock)
    machine = DailyStateMachine(conn, cfg, clock, pipeline, service)

    assert machine.tick().action == "applied"
    assert conn.execute(
        "select 1 from state where key='prepare_retry_at_2026-10-18'"
    ).fetchone() is not None
    assert conn.execute(
        "select 1 from state where key='prepared_2026-10-18'"
    ).fetchone() is None

    # The retry moment passes and every source is healthy again.
    conn.execute(
        "update state set value='0' where key='prepare_retry_at_2026-10-18'"
    )
    conn.execute("delete from state where key='source_down_Source_2026-10-18'")
    conn.commit()
    pipeline.sources = [Source(_candidates())]

    assert machine.tick().action == "already_changed"
    assert conn.execute(
        "select 1 from state where key='prepared_2026-10-18'"
    ).fetchone() is not None
