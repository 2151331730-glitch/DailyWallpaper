"""Guards against a dead network turning one click into a long freeze."""
import time
from datetime import datetime
from pathlib import Path

from dailywallpaper.app import (
    HOST_COOLDOWN_SECONDS,
    MATERIALIZE_ATTEMPTS,
    RealPipeline,
)
from dailywallpaper.db import connect, migrate
from dailywallpaper.models import Decision


def _decision(wid):
    return Decision(
        wallpaper_id=wid,
        image_path=Path("x.jpg"),
        scores=None,
        explore=0.0,
        personalize=0.0,
        final=0.0,
        reason="",
        source="wallhaven",
        apply_path=None,
    )


def test_a_failed_host_is_retried_after_the_cooldown():
    pipeline = RealPipeline.__new__(RealPipeline)
    pipeline._failed_hosts = {"w.wallhaven.cc": time.time()}

    assert pipeline._host_failed("w.wallhaven.cc") is True

    pipeline._failed_hosts["w.wallhaven.cc"] = (
        time.time() - HOST_COOLDOWN_SECONDS - 1
    )
    assert pipeline._host_failed("w.wallhaven.cc") is False


def test_pick_gives_up_after_repeated_download_failures(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    pipeline = RealPipeline.__new__(RealPipeline)
    pipeline.conn = conn
    pipeline.shortlist = [_decision(wid) for wid in range(1, 6)]
    pipeline._materialize_one = lambda decision: None

    assert pipeline.pick(datetime(2026, 10, 22, 9, 0)) is None
    assert len(pipeline.shortlist) == 5 - MATERIALIZE_ATTEMPTS
