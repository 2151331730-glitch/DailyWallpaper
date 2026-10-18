# src/dailywallpaper/cost/report.py
"""Read-only spend report for the cost tab (spec 10, 11.2)."""
from __future__ import annotations

from dataclasses import dataclass

MONTH_FILTER = "substr(ts,1,7)=? AND NOT (status_code BETWEEN 400 AND 499)"
RECENT_LIMIT = 20


@dataclass(frozen=True)
class ModelRow:
    model: str
    calls: int
    cost: float


@dataclass(frozen=True)
class CallRow:
    ts: str
    model: str
    purpose: str
    cost: float
    cache_hit: int


@dataclass(frozen=True)
class CostSummary:
    month: str
    spent: float
    calls: int
    cache_hits: int
    by_model: list
    recent: list

    def cache_hit_rate(self) -> float:
        return 0.0 if self.calls == 0 else self.cache_hits / self.calls


class CostService:
    """Aggregates this month's api_usage rows for the UI."""

    def __init__(self, conn, clock):
        self.conn = conn
        self.clock = clock

    def summary(self) -> CostSummary:
        month = self.clock.now().strftime("%Y-%m")
        spent = self.conn.execute(
            f"select coalesce(sum(cost_cny),0) from api_usage where {MONTH_FILTER}",
            (month,),
        ).fetchone()[0]
        calls = self.conn.execute(
            f"select count(*) from api_usage where {MONTH_FILTER}", (month,)
        ).fetchone()[0]
        hits = self.conn.execute(
            f"select count(*) from api_usage where {MONTH_FILTER} and cache_hit=1",
            (month,),
        ).fetchone()[0]
        by_model = [
            ModelRow(row[0], int(row[1]), float(row[2]))
            for row in self.conn.execute(
                f"""
                select model, count(*), coalesce(sum(cost_cny),0)
                from api_usage where {MONTH_FILTER}
                group by model order by 3 desc
                """,
                (month,),
            ).fetchall()
        ]
        recent = [
            CallRow(row[0], row[1], row[2], float(row[3]), int(row[4]))
            for row in self.conn.execute(
                """
                select ts, model, purpose, cost_cny, cache_hit from api_usage
                order by id desc limit ?
                """,
                (RECENT_LIMIT,),
            ).fetchall()
        ]
        return CostSummary(month, float(spent), int(calls), int(hits), by_model, recent)
