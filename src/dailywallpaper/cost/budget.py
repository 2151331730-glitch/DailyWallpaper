# src/dailywallpaper/cost/budget.py
"""Monthly spend guard backed by the api_usage ledger."""
from __future__ import annotations

from dailywallpaper.db import now_iso


class BudgetGuard:
    """Tracks this month's spend and refuses calls that would exceed the cap.

    A call is pre-written as an estimated row before it is made and settled
    afterwards, so a crash or a long request still shows up in ``spent()``.
    Client errors (4xx) are not billed and do not count.
    """

    def __init__(self, conn, monthly_cny: float, warn_ratio: float, clock, run_id: str = ""):
        self.conn = conn
        self.monthly_cny = monthly_cny
        self.warn_ratio = warn_ratio
        self.clock = clock
        self.run_id = run_id

    def _month(self) -> str:
        return self.clock.now().strftime("%Y-%m")

    def spent(self) -> float:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(cost_cny),0) FROM api_usage
            WHERE substr(ts,1,7)=? AND NOT (status_code BETWEEN 400 AND 499)
            """,
            (self._month(),),
        ).fetchone()
        return float(row[0])

    def check(self, estimate: float) -> bool:
        return self.spent() + estimate <= self.monthly_cny

    def warn_if_needed(self) -> bool:
        if self.spent() < self.monthly_cny * self.warn_ratio:
            return False
        key = f"budget_warned_{self._month()}"
        if self.conn.execute("select 1 from state where key=?", (key,)).fetchone():
            return False
        self.conn.execute("insert into state(key,value) values(?,?)", (key, "1"))
        self.conn.commit()
        return True

    def prewrite_estimate(self, provider, model, purpose, estimate) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO api_usage(ts,run_id,provider,model,purpose,tokens_in,tokens_out,
                                  cost_cny,estimated,cache_hit,status_code,latency_ms)
            VALUES(?,?,?,?,?,0,0,?,1,0,0,0)
            """,
            (now_iso(), self.run_id, provider, model, purpose, estimate),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def settle_usage(
        self, row_id, tokens_in, tokens_out, cost, status_code, latency_ms
    ) -> None:
        self.conn.execute(
            """
            UPDATE api_usage
            SET tokens_in=?, tokens_out=?, cost_cny=?, estimated=0,
                status_code=?, latency_ms=?
            WHERE id=?
            """,
            (tokens_in, tokens_out, cost, status_code, latency_ms, row_id),
        )
        if 400 <= status_code < 500:
            self.conn.execute("UPDATE api_usage SET cost_cny=0 WHERE id=?", (row_id,))
        self.conn.commit()
