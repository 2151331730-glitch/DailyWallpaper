# tests/test_integration.py
import io
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image

from dailywallpaper.analysis.vision import VisionCallResult, VisionClient, VisionScores
from dailywallpaper.app import Application, RealPipeline
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


def test_a_dynamic_day_only_keeps_scene_and_video(tmp_path):
    from datetime import date

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    scene = Candidate("we_workshop", "s1", "场景", "", "f", "https://img/day1/0.png",
                      "scene", 0, 0, 0, [], {})
    still = Candidate("bing", "i1", "图片", "", "f", "https://img/day1/1.png",
                      "image", 3840, 2160, 0, [], {})
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    budget = CountingBudget(100)
    from dataclasses import replace

    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="dynamic_only"))
    pipeline = RealPipeline(cfg, conn, SynthSession(), [Source([scene, still])], vision, budget)

    pipeline.prepare(datetime(2026, 10, 18, 9, 0))

    types = {row["media_type"] for row in conn.execute("select media_type from wallpapers")}
    assert types == {"scene"}
    assert len(pipeline.shortlist) == 1


def test_a_static_day_falls_back_when_only_dynamic_exists(tmp_path):
    from dataclasses import replace

    cfg = _config(tmp_path)
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    scene = Candidate("we_workshop", "s1", "场景", "", "f", "https://img/day1/0.png",
                      "scene", 0, 0, 0, [], {})
    vision = VisionClient(Provider([_scores(90, 10)]), conn, cfg.vision.pricing)
    cfg = replace(cfg, wallpaper=replace(cfg.wallpaper, media_strategy="static_only"))
    pipeline = RealPipeline(cfg, conn, SynthSession(), [Source([scene])], vision, CountingBudget(100))

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
