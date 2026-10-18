# tests/test_budget.py
from datetime import datetime

import pytest

from dailywallpaper.cost.budget import BudgetGuard
from dailywallpaper.db import connect, migrate


class _Clock:
    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment


def _usage(conn, ts, cost, status=200, estimated=0):
    conn.execute(
        """INSERT INTO api_usage(ts,run_id,provider,model,purpose,tokens_in,
           tokens_out,cost_cny,estimated,cache_hit,status_code,latency_ms)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, "", "zhipu", "m", "screen", 0, 0, cost, estimated, 0, status, 0),
    )
    conn.commit()


def _guard(tmp_path, monthly=10.0, warn=0.8, moment=datetime(2026, 10, 17, 9, 0)):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn, BudgetGuard(conn, monthly, warn, _Clock(moment))


def test_spent_sums_only_current_month(tmp_path):
    conn, guard = _guard(tmp_path)
    _usage(conn, "2026-10-01T08:00:00", 1.5)
    _usage(conn, "2026-09-30T23:59:59", 9.0)

    assert guard.spent() == pytest.approx(1.5)


def test_spent_excludes_4xx(tmp_path):
    conn, guard = _guard(tmp_path)
    _usage(conn, "2026-10-01T08:00:00", 1.0)
    _usage(conn, "2026-10-01T09:00:00", 2.0, status=429)

    assert guard.spent() == pytest.approx(1.0)


def test_check_blocks_over_cap(tmp_path):
    conn, guard = _guard(tmp_path, monthly=10.0)
    _usage(conn, "2026-10-01T08:00:00", 9.0)

    assert guard.check(0.5) is True
    assert guard.check(1.5) is False


def test_check_is_inclusive_at_the_cap(tmp_path):
    conn, guard = _guard(tmp_path, monthly=10.0)
    _usage(conn, "2026-10-01T08:00:00", 9.0)

    assert guard.check(1.0) is True


def test_warning_fires_once_per_month(tmp_path):
    conn, guard = _guard(tmp_path, monthly=10.0, warn=0.8)
    _usage(conn, "2026-10-01T08:00:00", 8.0)

    assert guard.warn_if_needed() is True
    assert guard.warn_if_needed() is False


def test_warning_silent_below_ratio(tmp_path):
    conn, guard = _guard(tmp_path, monthly=10.0, warn=0.8)
    _usage(conn, "2026-10-01T08:00:00", 1.0)

    assert guard.warn_if_needed() is False


def test_estimated_row_counts_toward_spend(tmp_path):
    conn, guard = _guard(tmp_path)
    _usage(conn, "2026-10-01T08:00:00", 2.0, status=0, estimated=1)

    assert guard.spent() == pytest.approx(2.0)


def test_prewrite_then_settle(tmp_path):
    conn, guard = _guard(tmp_path)

    row_id = guard.prewrite_estimate("zhipu", "m", "screen", 0.003)
    assert guard.spent() == pytest.approx(0.003)

    guard.settle_usage(row_id, 100, 50, 0.0011, 200, 42)

    row = conn.execute("select * from api_usage where id=?", (row_id,)).fetchone()
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
    assert row["cost_cny"] == pytest.approx(0.0011)
    assert row["estimated"] == 0
    assert row["status_code"] == 200
    assert row["latency_ms"] == 42
    assert guard.spent() == pytest.approx(0.0011)


def test_settle_zeroes_cost_on_4xx(tmp_path):
    conn, guard = _guard(tmp_path)
    row_id = guard.prewrite_estimate("zhipu", "m", "screen", 0.003)

    guard.settle_usage(row_id, 0, 0, 0.003, 429, 10)

    row = conn.execute("select * from api_usage where id=?", (row_id,)).fetchone()
    assert row["cost_cny"] == 0
