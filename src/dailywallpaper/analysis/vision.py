# src/dailywallpaper/analysis/vision.py
from dataclasses import dataclass, replace
import json
import re
import time
from pathlib import Path

from dailywallpaper import identity
from dailywallpaper.cost.ledger import (
    AnalysisRecord,
    actual_cost,
    record_usage,
)
from dailywallpaper.db import now_iso

from .prompts import PROMPT_VERSION, SYSTEM_PROMPT

# One slot per build: the uninstaller deletes the whole service in one call, so
# a shared slot took the source tree's key with the packaged build's.
KEYRING_SERVICE = identity.name()
# The slot predates the provider knob (`[vision] provider`): it is where the
# vision key lives, whatever vendor the config points at. Renaming it would
# orphan every key already pasted in.
KEYRING_ACCOUNT = "zhipu"

# OpenAI-compatible chat endpoints that accept image_url content. A config
# `base_url` wins; this is only what an empty one means.
PROVIDER_BASE_URLS = {
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "deepseek": "https://api.deepseek.com/v1",
}
# The provider a config that says nothing gets. Must match the shipped
# config.example.toml and VisionSection's defaults, or a fresh install asks
# for one vendor's key and then calls the other one's endpoint.
DEFAULT_PROVIDER = "deepseek"

# What the UI calls each vendor, and where a user goes to get a key. A key
# prompt that still said "智谱" while the config pointed at DeepSeek sent the
# user to the wrong website.
PROVIDER_LABELS = {
    "zhipu": "智谱",
    "deepseek": "DeepSeek",
}
PROVIDER_KEY_HINTS = {
    "zhipu": "open.bigmodel.cn → API Keys",
    "deepseek": "platform.deepseek.com → API keys",
}

RETRY_INSTRUCTION = "Return valid JSON only; style_tags must have at least one item."


def provider_label(provider: str = "") -> str:
    """The vendor's display name for the provider a config names."""
    name = (provider or DEFAULT_PROVIDER).strip().lower()
    return PROVIDER_LABELS.get(name, name or DEFAULT_PROVIDER)


def provider_key_hint(provider: str = "") -> str:
    """Where to apply for the provider's key."""
    name = (provider or DEFAULT_PROVIDER).strip().lower()
    return PROVIDER_KEY_HINTS.get(name, f"{provider_label(provider)} 官网 → API keys")


def resolve_base_url(provider: str = "", base_url: str = "") -> str:
    """The endpoint for a config, whatever half of it is filled in."""
    if base_url:
        return base_url
    return PROVIDER_BASE_URLS.get(
        (provider or DEFAULT_PROVIDER).strip().lower(),
        PROVIDER_BASE_URLS[DEFAULT_PROVIDER],
    )


def provider_from_config(cfg, api_key: str, **overrides) -> "OpenAIVision":
    """An OpenAIVision wired from ``[vision]``.

    The scripts used to build ``ZhipuVision(...)`` by hand, which quietly kept
    calling bigmodel no matter what the config said.
    """
    section = cfg.vision
    return OpenAIVision(
        api_key,
        max_output_tokens=overrides.get(
            "max_output_tokens", section.max_output_tokens
        ),
        temperature=overrides.get("temperature", section.temperature),
        base_url=resolve_base_url(section.provider, section.base_url),
        thinking=getattr(section, "thinking", False),
        provider_name=section.provider,
    )


def save_key(key: str) -> None:
    """Store the Zhipu API key in the OS credential store."""
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key)


def load_key() -> str | None:
    """Load the Zhipu API key from the OS credential store."""
    import keyring

    return keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)


class VisionError(RuntimeError):
    """A failed model call that may still have been billed.

    A 200 response that fails validation is charged by the provider, so the
    token counts travel with the error and the caller settles the ledger
    instead of recording a free call.
    """

    def __init__(self, message: str, tokens_in: int = 0, tokens_out: int = 0,
                 latency_ms: int = 0):
        super().__init__(message)
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.latency_ms = latency_ms

@dataclass(frozen=True)
class VisionScores:
    quality: int
    creativity: int
    novelty: int
    formulaic: int
    nsfw: bool
    watermark: bool
    text_overlay: str
    style_tags: list[str]
    mood_tags: list[str]
    palette: list[str]
    subject: str
    reason: str

@dataclass(frozen=True)
class VisionCallResult:
    scores: VisionScores
    tokens_in: int
    tokens_out: int
    latency_ms: int
    reasoning_tokens: int = 0

REQUIRED = {"quality","creativity","novelty","formulaic","nsfw","watermark",
            "text_overlay","style_tags","mood_tags","palette","subject","reason"}
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
MAX_TAG_LENGTH = 64

def _as_tags(value) -> list[str]:
    """Coerce a tag field to a list.

    The schema asks for a JSON array, but glm-4.6v-flash returns a
    comma-separated string. Accept both; a comma-separated string is
    normalized the same way a list is."""
    if isinstance(value, str):
        return [part.strip().lower() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        items = value
    else:
        raise VisionError("tags must be a list or a comma-separated string")
    if any(isinstance(item, str) and len(item) > MAX_TAG_LENGTH for item in items):
        raise VisionError("tag is too long")
    return items


def _as_palette(value) -> list[str]:
    """Coerce a palette field to a list of #RRGGBB strings.

    glm-4.6v-flash returns the palette as a comma-separated string of bare
    hex codes ("ff8c00, 8b4513"). The spec requires #RRGGBB, so the '#'
    is restored; a malformed value still fails validation. A model that
    returns six or seven colours is not a broken answer -- the caller
    truncates to the five the prompt asked for, because throwing away a
    paid analysis over a decorative field costs a whole candidate."""
    if isinstance(value, str):
        items: list = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        items = value
    else:
        raise VisionError("palette must be a list or a comma-separated string")
    palette = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise VisionError("bad palette")
        item = item.strip()
        if not item.startswith("#"):
            item = "#" + item
        if not HEX.match(item):
            raise VisionError("bad palette")
        palette.append(item)
    return palette


def _int(payload, key):
    v = payload[key]
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100:
        raise VisionError(f"{key} must be an integer 0-100")
    return v

def validate_scores(payload: dict) -> VisionScores:
    if not isinstance(payload, dict):
        raise VisionError("response is not an object")
    missing = REQUIRED - payload.keys()
    if missing:
        raise VisionError(f"missing keys: {sorted(missing)}")
    style = _as_tags(payload["style_tags"])
    if not style or len(style) > 8:
        raise VisionError("style_tags must be 1-8 items")
    if not all(isinstance(x, str) and x.strip() for x in style):
        raise VisionError("style_tags must be non-empty strings")
    if not isinstance(payload["nsfw"], bool) or not isinstance(payload["watermark"], bool):
        raise VisionError("nsfw and watermark must be booleans")
    mood = _as_tags(payload["mood_tags"])
    if len(mood) > 6 or not all(isinstance(x, str) for x in mood):
        raise VisionError("mood_tags must be a list of <=6 strings")
    overlay = payload["text_overlay"]
    if not isinstance(overlay, str) or overlay not in {"none","minor","major"}:
        raise VisionError("bad text_overlay")
    # The prompt asks for 0-5 colours; keep the first five if the model
    # volunteers more instead of discarding scores that are otherwise fine.
    palette = _as_palette(payload["palette"])[:5]
    return VisionScores(
        quality=_int(payload,"quality"),
        creativity=_int(payload,"creativity"),
        novelty=_int(payload,"novelty"),
        formulaic=_int(payload,"formulaic"),
        nsfw=bool(payload["nsfw"]),
        watermark=bool(payload["watermark"]),
        text_overlay=overlay,
        style_tags=[x.strip().lower() for x in style],
        mood_tags=[x.strip().lower() for x in mood],
        palette=palette,
        subject=str(payload["subject"])[:40],
        reason=str(payload["reason"])[:160],
    )


class OpenAIVision:
    """An OpenAI-compatible endpoint that reads an image and returns JSON.

    Zhipu's GLM-vision and DeepSeek's flash both speak this dialect; only the
    base URL, the model name and whether the model is allowed to reason
    differ, so those are constructor arguments rather than hard-coded.
    """

    BASE_URL = PROVIDER_BASE_URLS[DEFAULT_PROVIDER]
    # Class-level defaults keep hand-built instances (tests use __new__)
    # working; __init__ overrides them from the config.
    temperature = 0.2
    max_output_tokens = 500
    thinking = False
    def __init__(self, api_key: str, max_output_tokens: int = 500,
                 temperature: float = 0.2, base_url: str = "",
                 thinking: bool = False, provider_name: str = ""):
        # The config owns these; hard-coding them made the settings page's
        # values silently unused.
        self.max_output_tokens = int(max_output_tokens)
        self.temperature = float(temperature)
        self.thinking = bool(thinking)
        # Only used to label rows in the usage ledger.
        self.provider_name = provider_name or DEFAULT_PROVIDER
        # openai 3.x ships with httpx2 (the vendored successor of httpx).
        try:
            import httpx2 as httpx
        except ImportError:  # older openai
            import httpx  # type: ignore[no-redef]
        from openai import OpenAI

        # Both endpoints are domestic, so they are always reached directly.
        # The system proxy is Pigeon's business: when it points at a local
        # port that nothing listens on (7892 with the client disconnected),
        # every model call dies with "Connection error" while the site itself
        # is up.
        http_client = httpx.Client(trust_env=False, timeout=60.0)
        # A 429 here means the model is overloaded, not that this request was
        # unlucky: the library's two retries just tripled the wait, and the
        # pipeline already skips a failed candidate and moves on.
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url or self.BASE_URL,
            max_retries=0,
            http_client=http_client,
            # The SDK's own default is 600 s and it wins over the http_client
            # timeout; one stalled upload then blocked the whole tick thread
            # for ten minutes. Fail fast and let the candidate be skipped.
            timeout=60.0,
        )

    def analyze(
        self, image_path: Path, model: str, extra_instruction: str = ""
    ) -> VisionCallResult:
        import base64
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        system_prompt = (
            f"{SYSTEM_PROMPT}\n{extra_instruction}" if extra_instruction else SYSTEM_PROMPT
        )
        started = time.monotonic()
        resp = self.client.chat.completions.create(
            model=model,
            temperature=self.temperature,
            max_tokens=self.max_output_tokens,
            response_format={"type": "json_object"},
            # glm-4.6v-flash spends the whole output budget on reasoning by
            # default (finish_reason=length, empty content). Disabling it
            # leaves the budget for the JSON answer. DeepSeek accepts the same
            # switch; with `thinking` on the parameter is left out and the
            # model reasons first, which needs a much larger output budget.
            extra_body=None if self.thinking else {"thinking": {"type": "disabled"}},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "Score this wallpaper and return JSON."},
                ]},
            ],
        )
        # Usage is read before the payload: a 200 that fails validation was
        # still billed, and the caller needs the counts to record it.
        try:
            tokens_in = int(resp.usage.prompt_tokens or 0)
            tokens_out = int(resp.usage.completion_tokens or 0)
            details = getattr(resp.usage, "completion_tokens_details", None)
            reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0)
        except (AttributeError, KeyError, TypeError, ValueError):
            tokens_in = tokens_out = reasoning_tokens = 0
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            payload = json.loads(resp.choices[0].message.content)
        except (ValueError, TypeError, AttributeError, IndexError, KeyError) as exc:
            raise VisionError(
                "provider returned malformed response",
                tokens_in, tokens_out, latency_ms,
            ) from exc
        try:
            scores = validate_scores(payload)
        except VisionError as exc:
            raise VisionError(str(exc), tokens_in, tokens_out, latency_ms) from exc
        return VisionCallResult(
            scores=scores,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            reasoning_tokens=reasoning_tokens,
        )


# The pre-rename name: scripts and tests still import it. Same class, one
# implementation -- keeping the alias stops the provider switch from turning
# into a rename of every call site.
ZhipuVision = OpenAIVision

CACHE_HIT_PURPOSE = "screen"
ESTIMATE_CNY = 0.003


class VisionClient:
    """Cache-aware wrapper around a vision provider.

    One paid call per (image_hash, model, prompt_version). Seeing the same
    image again links the existing analysis to the new wallpaper and records
    a zero-cost cache hit instead of paying twice.
    """

    def __init__(self, provider, conn, pricing, budget=None, max_output_tokens: int = 500):
        self.provider = provider
        self.conn = conn
        self.pricing = pricing
        self.budget = budget
        self.max_output_tokens = max_output_tokens
        # The usage ledger labels rows by vendor; the provider knows its own
        # name, and a hand-built test double without one keeps the old label.
        self.provider_name = str(
            getattr(provider, "provider_name", "") or DEFAULT_PROVIDER
        )

    def _prewrite(self, model: str):
        if not self.budget:
            return None
        return self.budget.prewrite_estimate(
            provider=self.provider_name,
            model=model,
            purpose=CACHE_HIT_PURPOSE,
            estimate=ESTIMATE_CNY,
        )

    def _settle_failure(self, usage_id, model: str, exc: Exception) -> None:
        """Record a failed call with the tokens it actually spent.

        A validation failure comes back from a 200 response, so it is paid
        for; a network failure has no usage and is recorded as such.
        """
        tokens_in = int(getattr(exc, "tokens_in", 0) or 0)
        tokens_out = int(getattr(exc, "tokens_out", 0) or 0)
        latency_ms = int(getattr(exc, "latency_ms", 0) or 0)
        cost = actual_cost(tokens_in, tokens_out, self.pricing[model])
        status = 200 if tokens_in else 500
        if self.budget and usage_id is not None:
            self.budget.settle_usage(
                usage_id, tokens_in, tokens_out, cost, status, latency_ms
            )
        else:
            record_usage(
                self.conn,
                provider=self.provider_name,
                model=model,
                purpose=CACHE_HIT_PURPOSE,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_cny=cost,
                estimated=0,
                cache_hit=0,
                status_code=status,
                latency_ms=latency_ms,
            )

    def latest_for(self, wallpaper_id: int, model: str) -> AnalysisRecord | None:
        """What we already know about a wallpaper, without calling anyone.

        The hash cache misses when re-normalising an old file yields a new
        digest; the row link does not.
        """
        row = self.conn.execute(
            """
            select a.* from wallpaper_analyses wa
            join analyses a on a.id = wa.analysis_id
            where wa.wallpaper_id = ? and a.model = ?
            order by a.id desc limit 1
            """,
            (wallpaper_id, model),
        ).fetchone()
        return None if row is None else self._cached(row, wallpaper_id, model)

    def latest_any_for(self, wallpaper_id: int) -> AnalysisRecord | None:
        """The newest analysis of a wallpaper under *any* model.

        The model name is part of the cache key, so switching provider leaves
        almost no history scored under the new one. Anything that merely wants
        "what did we already decide about this image" has to fall back to an
        older model's opinion or it goes blind exactly at the moment the user
        is waiting (see ``SelectMixin.repeat_pick``).
        """
        row = self.conn.execute(
            """
            select a.* from wallpaper_analyses wa
            join analyses a on a.id = wa.analysis_id
            where wa.wallpaper_id = ?
            order by a.id desc limit 1
            """,
            (wallpaper_id,),
        ).fetchone()
        if row is None:
            return None
        return self._cached(row, wallpaper_id, row["model"])

    def _cached(self, row, wallpaper_id: int, model: str) -> AnalysisRecord:
        self.conn.execute(
            "INSERT OR IGNORE INTO wallpaper_analyses(wallpaper_id,analysis_id,created_at)"
            " VALUES(?,?,?)",
            (wallpaper_id, row["id"], now_iso()),
        )
        self.conn.commit()
        record_usage(
            self.conn,
            provider=self.provider_name,
            model=model,
            purpose=CACHE_HIT_PURPOSE,
            tokens_in=0,
            tokens_out=0,
            cost_cny=0.0,
            estimated=0,
            cache_hit=1,
            status_code=0,
            latency_ms=0,
        )
        # cost_cny on the record means "what this call cost", so a cache hit is 0.
        return replace(AnalysisRecord.from_row(row, cached=True), cost_cny=0.0)

    def analyze_or_cache(
        self, wallpaper_id: int, image_hash: str, image_path: Path, model: str
    ) -> AnalysisRecord:
        row = self.conn.execute(
            "SELECT * FROM analyses WHERE image_hash=? AND model=? AND prompt_version=?",
            (image_hash, model, PROMPT_VERSION),
        ).fetchone()
        if row:
            return self._cached(row, wallpaper_id, model)

        usage_id = self._prewrite(model)
        try:
            try:
                result = self.provider.analyze(image_path, model)
            except VisionError as first:
                # A 200 that failed validation was still billed. Settle that
                # attempt before paying for the retry.
                self._settle_failure(usage_id, model, first)
                usage_id = self._prewrite(model)
                result = self.provider.analyze(
                    image_path, model, extra_instruction=RETRY_INSTRUCTION
                )
        except Exception as exc:
            self._settle_failure(usage_id, model, exc)
            raise

        cost = actual_cost(result.tokens_in, result.tokens_out, self.pricing[model])
        scores = result.scores
        cursor = self.conn.execute(
            """
            INSERT INTO analyses(model,prompt_version,image_hash,scores_json,quality,creativity,
              novelty,formulaic,nsfw,style_tags_json,mood_tags_json,palette_json,
              tokens_in,tokens_out,cost_cny,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                model,
                PROMPT_VERSION,
                image_hash,
                json.dumps(scores.__dict__, ensure_ascii=False),
                scores.quality,
                scores.creativity,
                scores.novelty,
                scores.formulaic,
                int(scores.nsfw),
                json.dumps(scores.style_tags),
                json.dumps(scores.mood_tags),
                json.dumps(scores.palette),
                result.tokens_in,
                result.tokens_out,
                cost,
                now_iso(),
            ),
        )
        analysis_id = int(cursor.lastrowid)
        self.conn.execute(
            "INSERT INTO wallpaper_analyses(wallpaper_id,analysis_id,created_at) VALUES(?,?,?)",
            (wallpaper_id, analysis_id, now_iso()),
        )
        self.conn.commit()

        if self.budget and usage_id is not None:
            # The pre-written row *is* this call's usage record; settling it
            # is the whole write. Inserting record_usage as well would make
            # spent() count the same cost twice.
            self.budget.settle_usage(
                usage_id, result.tokens_in, result.tokens_out, cost, 200, result.latency_ms
            )
        else:
            record_usage(
                self.conn,
                provider=self.provider_name,
                model=model,
                purpose=CACHE_HIT_PURPOSE,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_cny=cost,
                estimated=0,
                cache_hit=0,
                status_code=200,
                latency_ms=result.latency_ms,
            )

        stored = self.conn.execute(
            "SELECT * FROM analyses WHERE id=?", (analysis_id,)
        ).fetchone()
        return AnalysisRecord.from_row(stored)
