# tests/test_prepare_next.py
"""The warm-up pass parks the next wallpaper so a boot can apply it at once."""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from dailywallpaper.analysis.vision import VisionScores
from dailywallpaper.config import AppSection
from dailywallpaper.db import connect, migrate
from dailywallpaper.models import Decision
from dailywallpaper.pipeline.select import SelectMixin
from dailywallpaper.state.machine import PREPARED_KEY


class _Pipeline(SelectMixin):
    def __init__(self, conn, decisions):
        self.conn = conn
        self.cfg = SimpleNamespace(app=AppSection())
        self.shortlist = list(decisions)
        self.last_pick_reason = ""
        self.prepared = 0

    def prepare(self, now, force=False, dry_run=False):
        self.prepared += 1

    def _materialize_one(self, decision):
        return decision


def _scores():
    return VisionScores(
        quality=80, creativity=70, novelty=60, formulaic=20, nsfw=False,
        watermark=False, text_overlay="none", style_tags=["landscape"],
        mood_tags=["calm"], palette=["#112233"], subject="s", reason="r",
    )


def _decision(wid, path):
    return Decision(
        wallpaper_id=wid, image_path=path, scores=_scores(),
        explore=60.0, personalize=60.0, final=60.0, reason="pick",
        apply_path=path,
    )


def _setup(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    conn.commit()
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    image = tmp_path / f"{wid}.jpg"
    image.write_bytes(b"jpeg")
    return conn, wid, image


def _payload(conn):
    row = conn.execute("select value from state where key=?", (PREPARED_KEY,)).fetchone()
    return None if row is None else json.loads(row[0])


def test_prepare_next_parks_a_decision(tmp_path):
    conn, wid, image = _setup(tmp_path)
    pipeline = _Pipeline(conn, [_decision(wid, image)])

    assert pipeline.prepare_next(datetime(2026, 10, 22, 20, 0)) is True

    parked = _payload(conn)
    assert parked["wallpaper_id"] == wid
    assert parked["apply_path"] == str(image)
    assert parked["decision_id"] is not None
    assert parked["media_kind"] == "image"
    assert conn.execute(
        "select status from decisions where id=?", (parked["decision_id"],)
    ).fetchone()[0] == "shortlisted"


def test_prepare_next_keeps_the_existing_pick(tmp_path):
    conn, wid, image = _setup(tmp_path)
    pipeline = _Pipeline(conn, [_decision(wid, image)])

    assert pipeline.prepare_next(datetime(2026, 10, 22, 20, 0)) is True
    first = _payload(conn)

    assert pipeline.prepare_next(datetime(2026, 10, 22, 20, 5)) is True

    assert _payload(conn)["decision_id"] == first["decision_id"]
    assert conn.execute("select count(*) from decisions").fetchone()[0] == 1


def test_a_vanished_pick_is_superseded_by_a_fresh_one(tmp_path):
    conn, wid, image = _setup(tmp_path)
    second = tmp_path / "second.jpg"
    second.write_bytes(b"jpeg")
    pipeline = _Pipeline(conn, [_decision(wid, image), _decision(wid, second)])

    assert pipeline.prepare_next(datetime(2026, 10, 22, 20, 0)) is True
    first = _payload(conn)
    Path(first["apply_path"]).unlink()

    assert pipeline.prepare_next(datetime(2026, 10, 22, 21, 0)) is True

    assert _payload(conn)["decision_id"] != first["decision_id"]
    assert conn.execute(
        "select status from decisions where id=?", (first["decision_id"],)
    ).fetchone()[0] == "superseded"


def test_prepare_next_with_an_empty_shortlist_reports_failure(tmp_path):
    conn, wid, image = _setup(tmp_path)
    pipeline = _Pipeline(conn, [])

    assert pipeline.prepare_next(datetime(2026, 10, 22, 20, 0)) is False
    assert _payload(conn) is None
