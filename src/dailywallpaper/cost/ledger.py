# src/dailywallpaper/cost/ledger.py
"""Usage ledger: every paid call and every cache hit gets a row."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dailywallpaper.db import now_iso

if TYPE_CHECKING:
    from dailywallpaper.analysis.vision import VisionScores


def estimate_cost(tokens_in: int, max_output_tokens: int, pricing) -> float:
    """Upper-bound cost in CNY for one call, using the config price table."""
    return (
        tokens_in * pricing.input_cny_per_million
        + max_output_tokens * pricing.output_cny_per_million
    ) / 1_000_000


def actual_cost(tokens_in: int, tokens_out: int, pricing) -> float:
    """What a call really cost, from the usage the provider reported."""
    return (
        tokens_in * pricing.input_cny_per_million
        + tokens_out * pricing.output_cny_per_million
    ) / 1_000_000


def record_usage(
    conn,
    provider: str,
    model: str,
    purpose: str,
    tokens_in: int,
    tokens_out: int,
    cost_cny: float,
    estimated: int,
    cache_hit: int,
    status_code: int,
    latency_ms: int,
    run_id: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO api_usage(ts,run_id,provider,model,purpose,tokens_in,tokens_out,
          cost_cny,estimated,cache_hit,status_code,latency_ms)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            now_iso(),
            run_id,
            provider,
            model,
            purpose,
            tokens_in,
            tokens_out,
            cost_cny,
            estimated,
            cache_hit,
            status_code,
            latency_ms,
        ),
    )
    conn.commit()


@dataclass(frozen=True)
class AnalysisRecord:
    id: int
    scores: "VisionScores"
    tokens_in: int
    tokens_out: int
    cost_cny: float
    cached: bool = False
    # Which rubric produced this row; an old version judged by different
    # criteria and should not stand in for a current one.
    prompt_version: str = ""

    @classmethod
    def from_row(cls, row, cached: bool = False) -> "AnalysisRecord":
        # Imported here to avoid a module-level cycle with analysis.vision.
        from dailywallpaper.analysis.vision import validate_scores

        try:
            version = row["prompt_version"] or ""
        except (KeyError, IndexError):
            version = ""
        return cls(
            id=row["id"],
            scores=validate_scores(json.loads(row["scores_json"])),
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            cost_cny=row["cost_cny"],
            cached=cached,
            prompt_version=version,
        )
