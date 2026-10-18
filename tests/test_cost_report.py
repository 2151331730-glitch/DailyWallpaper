# tests/test_cost_report.py
from datetime import datetime

from dailywallpaper.cost.report import CostService
from dailywallpaper.db import connect, migrate


class _Clock:
    def now(self):
        return datetime(2026, 10, 18, 12, 0)


def _usage(conn, ts, cost, model="glm-4.6v-flash", cache=0, status=200):
    conn.execute(
        "insert into api_usage(ts,provider,model,purpose,tokens_in,tokens_out,cost_cny,"
        "estimated,cache_hit,status_code,latency_ms) values(?,?,?,?,0,0,?,0,?,?,0)",
        (ts, "zhipu", model, "screen", cost, cache, status),
    )
    conn.commit()


def _service(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    return conn, CostService(conn, _Clock())


def test_empty_ledger_is_all_zero(tmp_path):
    conn, service = _service(tmp_path)
    summary = service.summary()
    assert summary.spent == 0.0
    assert summary.calls == 0
    assert summary.cache_hit_rate() == 0.0


def test_only_the_current_month_counts(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5)
    _usage(conn, "2026-09-30T09:00:00", 9.0)
    assert service.summary().spent == 0.5


def test_4xx_rows_are_excluded_from_spend(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5)
    _usage(conn, "2026-10-02T10:00:00", 3.0, status=429)
    assert service.summary().spent == 0.5


def test_groups_by_model(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5, model="a")
    _usage(conn, "2026-10-02T09:10:00", 0.2, model="a")
    _usage(conn, "2026-10-02T09:20:00", 0.3, model="b")
    rows = {row.model: row for row in service.summary().by_model}
    assert rows["a"].calls == 2
    assert rows["a"].cost == 0.7
    assert rows["b"].calls == 1


def test_cache_hit_rate_counts_both_kinds(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5, cache=0)
    _usage(conn, "2026-10-02T09:10:00", 0.0, cache=1)
    _usage(conn, "2026-10-02T09:20:00", 0.0, cache=1)
    assert service.summary().cache_hit_rate() > 0.6


def test_recent_calls_are_newest_first(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5)
    _usage(conn, "2026-10-03T09:00:00", 0.6)
    assert service.summary().recent[0].cost == 0.6
