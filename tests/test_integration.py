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


def _config(tmp_path):
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

[vision.pricing."{MODEL}"]
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
    def __init__(self):
        self.images = {}
        for index, cell in enumerate((8, 16, 32)):
            buffer = io.BytesIO()
            _checkerboard(cell).save(buffer, "PNG")
            self.images[f"https://img/{index}.png"] = buffer.getvalue()

    def get(self, url, stream=True, timeout=30):
        return _Response(self.images[url])


class Source:
    def __init__(self, candidates):
        self.candidates = candidates

    def fetch(self, limit=None):
        return list(self.candidates)


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
    provider = Provider([_scores(90, 10), _scores(50, 20), _scores(50, 20)])
    vision = VisionClient(provider, conn, cfg.vision.pricing)
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    pipeline = RealPipeline(cfg, conn, ImageSession(), [Source(_candidates())], vision, budget)
    service = ApplyService(conn, Applier(), clock=clock)
    machine = DailyStateMachine(conn, cfg, clock, pipeline, service)
    app = Application(machine, sleep=lambda seconds: None)

    assert app.run_once().action == "applied"
    assert app.run_once().action == "already_changed"

    for _ in range(6):
        clock.advance(1)
        assert app.run_once().action == "applied"

    rows = conn.execute(
        "select change_date from change_log where success=1 order by change_date"
    ).fetchall()
    assert len(rows) == 7
    assert len({row["change_date"] for row in rows}) == 7


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
