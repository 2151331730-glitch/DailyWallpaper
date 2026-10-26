# src/dailywallpaper/recommend/intents.py
"""Turn a rating comment into something the recommender can act on.

A comment can mean three different things, and they take different routes:

- local       "too dark"        -> adjust this picture's traits
- preference  "I like surreal"  -> lasting boost/avoid tags
- search      "want Ronaldo"    -> change what the sources fetch

The classifier is a text-only model call; the caller runs it off the UI
thread. Stored intents are permanent until removed from the today page.
"""
from __future__ import annotations

import json
import logging
import time

from dailywallpaper.analysis.vision import DEFAULT_PROVIDER
from dailywallpaper.db import now_iso

logger = logging.getLogger(__name__)

KINDS = ("local", "preference", "search", "action", "conditional")
DEFAULT_MODEL = "deepseek-flash"
# The same upper bound the vision path uses; classification is a much smaller
# call, but the budget guard needs a number before the call happens.
CLASSIFY_ESTIMATE_CNY = 0.003

CLASSIFY_PROMPT = """You turn one user note about a desktop wallpaper into JSON.
Return JSON only, no prose:
{
  "kind": "local" | "preference" | "search" | "action" | "conditional",
  "polarity": 1 or -1,
  "tags": ["lowercase english visual tags"],
  "query_zh": "",
  "query_en": "",
  "confidence": 0.0-1.0
}

Definitions:
- local: a judgement of THIS picture only, e.g. "too dark", "composition is crowded".
- preference: a lasting taste, e.g. "I like surreal", "no anime".
- search: the user wants to SEE a concrete subject, person or place, e.g.
  "want Ronaldo", "snow mountains".
- action: an instruction, e.g. "change it now".
- conditional: depends on weather or time, e.g. "dark when it rains".

Rules:
- polarity 1 = wants more of it, -1 = wants less.
- tags are concrete visual traits in English (fog, neon, portrait, ...), at most 5.
- For search, query_zh is the keyword in the user's own language and query_en
  the English equivalent; tags may be empty.
- If unsure, use kind "preference" with confidence <= 0.5.
"""


def classify_note(text: str, api_key: str, model: str = DEFAULT_MODEL,
                  client=None, budget=None, base_url: str = "",
                  provider: str = "") -> dict | None:
    """Return a validated intent dict, or None when the model cannot decide.

    ``client`` is injectable so tests can exercise the parse path without a
    network call. ``budget`` (a BudgetGuard) makes the call count against the
    monthly cap like every other model request; without it a paid model could
    be used here without the cap ever noticing.
    """
    text = (text or "").strip()
    if not text:
        return None
    # The request and the ledger must name the same provider; resolving it
    # once here keeps a defaulted call from billing the wrong vendor.
    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    usage_id = None
    if budget is not None:
        try:
            usage_id = budget.prewrite_estimate(
                provider=provider, model=model, purpose="classify",
                estimate=CLASSIFY_ESTIMATE_CNY,
            )
        except Exception:
            usage_id = None
    started = time.monotonic()
    if client is None:
        from openai import OpenAI

        try:
            import httpx2 as httpx
        except ImportError:  # older openai
            import httpx  # type: ignore[no-redef]

        from dailywallpaper.analysis.vision import resolve_base_url

        client = OpenAI(
            api_key=api_key,
            base_url=resolve_base_url(provider, base_url),
            max_retries=0,
            http_client=httpx.Client(trust_env=False, timeout=30.0),
            timeout=30.0,  # the SDK default (600 s) would outlast the dialog
        )
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.1,
            max_tokens=300,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFY_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        payload = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        logger.warning(f"intent classification failed: {exc}")
        if usage_id is not None:
            try:
                budget.settle_usage(
                    usage_id, 0, 0, 0.0, 500,
                    int((time.monotonic() - started) * 1000),
                )
            except Exception:
                pass
        return None
    if usage_id is not None:
        try:
            usage = getattr(response, "usage", None)
            budget.settle_usage(
                usage_id,
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
                CLASSIFY_ESTIMATE_CNY,
                200,
                int((time.monotonic() - started) * 1000),
            )
        except Exception:
            pass
    return _validate(payload)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _validate(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in KINDS:
        return None
    if kind in ("action", "conditional"):
        # Stored for display, but neither changes the pool or the score yet.
        return {
            "kind": kind,
            "polarity": _as_int(payload.get("polarity"), 1),
            "tags": [],
            "query_zh": "",
            "query_en": "",
            "confidence": _as_float(payload.get("confidence"), 0.5),
        }
    tags = [
        str(tag).strip().lower()
        for tag in (payload.get("tags") or [])
        if isinstance(tag, str) and tag.strip()
    ][:5]
    intent = {
        "kind": kind,
        "polarity": -1 if _as_int(payload.get("polarity"), 1) < 0 else 1,
        "tags": tags,
        "query_zh": str(payload.get("query_zh") or "").strip(),
        "query_en": str(payload.get("query_en") or "").strip(),
        "confidence": max(0.0, min(1.0, _as_float(payload.get("confidence"), 0.5))),
    }
    if kind == "search" and not (intent["query_zh"] or intent["query_en"]):
        return None
    return intent


def store(conn, intent: dict, wallpaper_id: int | None, text: str) -> int:
    # Re-saving the same sentence (or asking for the same search again) must
    # not stack duplicate rows: every duplicate would take another share of
    # the search budget.
    for existing in list_intents(conn):
        if existing["kind"] != intent["kind"]:
            continue
        payload = existing.get("payload") or {}
        if intent["kind"] == "search":
            if (payload.get("query_zh") or payload.get("query_en")) == (
                intent.get("query_zh") or intent.get("query_en")
            ):
                return existing["id"]
        elif intent["kind"] == "local":
            if (
                payload.get("tags") == intent.get("tags")
                and existing["wallpaper_id"] == wallpaper_id
            ):
                return existing["id"]
        elif (
            payload.get("tags") == intent.get("tags")
            and _as_int(payload.get("polarity"), 1)
            == _as_int(intent.get("polarity"), 1)
        ):
            return existing["id"]
    cursor = conn.execute(
        "insert into user_intents(kind,payload_json,wallpaper_id,source_comment,"
        "created_at) values(?,?,?,?,?)",
        (
            intent["kind"],
            json.dumps(intent, ensure_ascii=False),
            wallpaper_id,
            (text or "")[:200],
            now_iso(),
        ),
    )
    # The worker owns the shortlist; a state row tells it to rebuild, so the
    # dialog never touches another thread's data.
    conn.execute(
        "insert or replace into state(key,value) values('pool_dirty','1')"
    )
    conn.commit()
    return int(cursor.lastrowid)


def list_intents(conn) -> list[dict]:
    rows = conn.execute(
        "select id, kind, payload_json, wallpaper_id, source_comment, created_at"
        " from user_intents order by id desc"
    ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            payload = {}
        out.append(
            {
                "id": int(row["id"]),
                "kind": row["kind"],
                "payload": payload,
                "wallpaper_id": row["wallpaper_id"],
                "comment": row["source_comment"],
                "created_at": row["created_at"],
            }
        )
    return out


def remove_intent(conn, intent_id: int) -> None:
    conn.execute("delete from user_intents where id=?", (intent_id,))
    conn.commit()


def preference_terms(conn) -> tuple[list[str], list[str], list[str]]:
    """(boost_tags, avoid_tags, soft_avoid_tags) from local/preference notes.

    A "local" note judges one picture ("too dark"), so its negative tags only
    soften similar pictures; a "preference" note is a lasting taste and gets
    the full weight.
    """
    boost: list[str] = []
    avoid: list[str] = []
    soft: list[str] = []
    for row in conn.execute(
        "select kind, payload_json from user_intents"
        " where kind in ('local','preference')"
    ).fetchall():
        try:
            payload = json.loads(row[1] or "{}")
        except ValueError:
            continue
        tags = [
            tag.strip().lower()
            for tag in (payload.get("tags") or [])
            if isinstance(tag, str) and tag.strip()
        ]
        positive = _as_int(payload.get("polarity"), 1) > 0
        if row[0] == "local":
            (boost if positive else soft).extend(tags)
        elif positive:
            boost.extend(tags)
        else:
            avoid.extend(tags)
    return boost, avoid, soft


def search_queries(conn) -> list[dict]:
    """Every active search intent, newest first."""
    out = []
    for row in conn.execute(
        "select id, payload_json, source_comment from user_intents"
        " where kind='search' order by id desc"
    ).fetchall():
        try:
            payload = json.loads(row[1] or "{}")
        except ValueError:
            continue
        out.append(
            {
                "id": int(row[0]),
                "query_zh": payload.get("query_zh", ""),
                "query_en": payload.get("query_en", ""),
                "comment": row[2],
            }
        )
    return out
