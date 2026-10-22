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

from dailywallpaper.db import now_iso

logger = logging.getLogger(__name__)

KINDS = ("local", "preference", "search", "action", "conditional")
DEFAULT_MODEL = "glm-4.6v-flash"

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


def classify_note(text: str, api_key: str, model: str = DEFAULT_MODEL) -> dict | None:
    """Return a validated intent dict, or None when the model cannot decide."""
    text = (text or "").strip()
    if not text:
        return None
    from openai import OpenAI

    try:
        import httpx2 as httpx
    except ImportError:  # older openai
        import httpx  # type: ignore[no-redef]

    client = OpenAI(
        api_key=api_key,
        base_url="https://open.bigmodel.cn/api/paas/v4",
        max_retries=0,
        http_client=httpx.Client(trust_env=False, timeout=30.0),
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
        return None
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


def preference_terms(conn) -> tuple[list[str], list[str]]:
    """(boost_tags, avoid_tags) learned from local/preference intents."""
    boost: list[str] = []
    avoid: list[str] = []
    for row in conn.execute(
        "select payload_json from user_intents where kind in ('local','preference')"
    ).fetchall():
        try:
            payload = json.loads(row[0] or "{}")
        except ValueError:
            continue
        target = boost if int(payload.get("polarity") or 1) > 0 else avoid
        for tag in payload.get("tags") or []:
            if isinstance(tag, str) and tag.strip():
                target.append(tag.strip().lower())
    return boost, avoid


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
