# tests/test_vision_cache.py
import pytest

from dailywallpaper.analysis.vision import (
    VisionCallResult,
    VisionClient,
    VisionError,
    VisionScores,
)
from dailywallpaper.config import VisionPricing
from dailywallpaper.cost.ledger import estimate_cost
from dailywallpaper.db import connect, migrate


def _scores():
    return VisionScores(
        quality=80,
        creativity=70,
        novelty=60,
        formulaic=20,
        nsfw=False,
        watermark=False,
        text_overlay="none",
        style_tags=["landscape"],
        mood_tags=["calm"],
        palette=["#112233"],
        subject="s",
        reason="r",
    )


def _result(tokens_in=100, tokens_out=50, latency_ms=10):
    return VisionCallResult(
        scores=_scores(), tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms
    )


def _wallpaper(conn, source_id="1"):
    conn.execute(
        "insert into wallpapers(source,source_id,created_at) values('t',?,'x')", (source_id,)
    )
    conn.commit()
    return conn.execute("select id from wallpapers order by id desc limit 1").fetchone()[0]


def _pricing():
    return {"m": VisionPricing(input_cny_per_million=1.0, output_cny_per_million=2.0)}


class _CountingProvider:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def analyze(self, image_path, model, extra_instruction=""):
        self.calls += 1
        return self.result


class _FlakyProvider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def analyze(self, image_path, model, extra_instruction=""):
        self.calls.append(extra_instruction)
        if not extra_instruction:
            raise VisionError("provider returned malformed response")
        return self.result


class _AlwaysBadProvider:
    def __init__(self):
        self.calls = 0

    def analyze(self, image_path, model, extra_instruction=""):
        self.calls += 1
        raise VisionError("provider returned malformed response")


def test_estimate_cost():
    pricing = VisionPricing(input_cny_per_million=1.0, output_cny_per_million=2.0)

    assert estimate_cost(1_000_000, 1_000_000, pricing) == pytest.approx(3.0)
    assert estimate_cost(0, 0, pricing) == 0.0


def test_second_call_is_served_from_cache(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    # Two different wallpapers that resolve to the same image: the second is
    # a cache hit. (Linking the *same* wallpaper twice is a no-op: the
    # wallpaper_analyses primary key is (wallpaper_id, analysis_id).)
    first_wid = _wallpaper(conn, "1")
    second_wid = _wallpaper(conn, "2")
    provider = _CountingProvider(_result())
    client = VisionClient(provider, conn, _pricing())

    first = client.analyze_or_cache(first_wid, "h1", tmp_path / "x.jpg", "m")
    second = client.analyze_or_cache(second_wid, "h1", tmp_path / "x.jpg", "m")

    assert provider.calls == 1
    assert first.cached is False
    assert second.cached is True
    assert second.cost_cny == 0
    assert second.tokens_in == 100
    assert conn.execute("select count(*) from wallpaper_analyses").fetchone()[0] == 2
    assert conn.execute("select count(*) from api_usage where cache_hit=1").fetchone()[0] == 1


def test_success_records_usage_and_cost(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    wid = _wallpaper(conn)
    client = VisionClient(_CountingProvider(_result()), conn, _pricing())

    client.analyze_or_cache(wid, "h1", tmp_path / "x.jpg", "m")

    row = conn.execute("select * from api_usage where cache_hit=0").fetchone()
    assert row["purpose"] == "screen"
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
    assert row["status_code"] == 200
    assert row["cost_cny"] == pytest.approx(estimate_cost(100, 500, _pricing()["m"]))


def test_retries_once_with_stricter_instruction(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    wid = _wallpaper(conn)
    provider = _FlakyProvider(_result())
    client = VisionClient(provider, conn, _pricing())

    record = client.analyze_or_cache(wid, "h2", tmp_path / "x.jpg", "m")

    assert record.cached is False
    assert len(provider.calls) == 2
    assert provider.calls[0] == ""
    assert "style_tags" in provider.calls[1]


def test_two_failures_raise_and_insert_nothing(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    wid = _wallpaper(conn)
    provider = _AlwaysBadProvider()
    client = VisionClient(provider, conn, _pricing())

    with pytest.raises(VisionError):
        client.analyze_or_cache(wid, "h3", tmp_path / "x.jpg", "m")

    assert provider.calls == 2
    assert conn.execute("select count(*) from analyses").fetchone()[0] == 0
    assert conn.execute("select count(*) from wallpaper_analyses").fetchone()[0] == 0


def test_cached_scores_round_trip(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    wid = _wallpaper(conn)
    client = VisionClient(_CountingProvider(_result()), conn, _pricing())

    client.analyze_or_cache(wid, "h4", tmp_path / "x.jpg", "m")
    cached = client.analyze_or_cache(wid, "h4", tmp_path / "x.jpg", "m")

    assert cached.scores.style_tags == ["landscape"]
    assert cached.scores.palette == ["#112233"]
