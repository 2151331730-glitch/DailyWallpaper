# tests/test_media_history.py
import json
from datetime import date, timedelta

from dailywallpaper.db import connect, migrate
from dailywallpaper.state import media_history
from dailywallpaper.state.media_history import (
    HISTORY_KEY,
    kind_for_media,
    load_history,
    record_applied,
)

DAY = date(2026, 10, 18)


def _conn(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn


def test_kind_for_media_type():
    assert kind_for_media("scene") == "dynamic"
    assert kind_for_media("video") == "dynamic"
    assert kind_for_media("image") == "static"
    assert kind_for_media(None) == "static"


def test_a_recorded_day_is_read_back(tmp_path):
    conn = _conn(tmp_path)

    record_applied(conn, DAY, "dynamic")

    assert load_history(conn) == [(DAY.isoformat(), "dynamic")]


def test_recording_the_same_day_twice_replaces_the_kind(tmp_path):
    conn = _conn(tmp_path)

    record_applied(conn, DAY, "dynamic")
    record_applied(conn, DAY, "static")

    assert load_history(conn) == [(DAY.isoformat(), "static")]


def test_records_are_kept_in_date_order(tmp_path):
    conn = _conn(tmp_path)

    record_applied(conn, DAY, "static")
    record_applied(conn, DAY - timedelta(days=1), "dynamic")

    assert load_history(conn) == [
        ((DAY - timedelta(days=1)).isoformat(), "dynamic"),
        (DAY.isoformat(), "static"),
    ]


def test_old_days_are_trimmed(tmp_path):
    conn = _conn(tmp_path)
    for offset in range(media_history.KEEP_DAYS + 5, -1, -1):
        record_applied(conn, DAY - timedelta(days=offset), "static")

    history = load_history(conn)

    assert len(history) == media_history.KEEP_DAYS
    assert history[-1] == (DAY.isoformat(), "static")


def test_malformed_state_reads_as_empty(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "insert into state(key,value) values(?,?)", (HISTORY_KEY, "not json")
    )
    conn.commit()

    assert load_history(conn) == []


def test_bad_entries_are_dropped_not_fatal(tmp_path):
    conn = _conn(tmp_path)
    conn.execute(
        "insert into state(key,value) values(?,?)",
        (HISTORY_KEY, json.dumps([["2026-10-17", "dynamic"], ["x"], "y"])),
    )
    conn.commit()

    assert load_history(conn) == [("2026-10-17", "dynamic")]
