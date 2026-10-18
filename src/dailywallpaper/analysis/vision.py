# src/dailywallpaper/analysis/vision.py
from dataclasses import dataclass
import json
import re
import time
from pathlib import Path

from .prompts import SYSTEM_PROMPT

KEYRING_SERVICE = "DailyWallpaper"
KEYRING_ACCOUNT = "zhipu"


def save_key(key: str) -> None:
    """Store the Zhipu API key in the OS credential store."""
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key)


def load_key() -> str | None:
    """Load the Zhipu API key from the OS credential store."""
    import keyring

    return keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)


class VisionError(RuntimeError): ...

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

def _as_tags(value) -> list[str]:
    """Coerce a tag field to a list.

    The schema asks for a JSON array, but glm-4.6v-flash returns a
    comma-separated string. Accept both; a comma-separated string is
    normalized the same way a list is."""
    if isinstance(value, str):
        return [part.strip().lower() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return value
    raise VisionError("tags must be a list or a comma-separated string")


def _as_palette(value) -> list[str]:
    """Coerce a palette field to a list of #RRGGBB strings.

    glm-4.6v-flash returns the palette as a comma-separated string of bare
    hex codes ("ff8c00, 8b4513"). The spec requires #RRGGBB, so the '#'
    is restored; a malformed value still fails validation."""
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
    palette = _as_palette(payload["palette"])
    if len(palette) > 5:
        raise VisionError("bad palette")
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
        reason=str(payload["reason"])[:80],
    )


class ZhipuVision:
    BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
    def __init__(self, api_key: str):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, base_url=self.BASE_URL)

    def analyze(self, image_path: Path, model: str) -> VisionCallResult:
        import base64
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        started = time.monotonic()
        resp = self.client.chat.completions.create(
            model=model,
            temperature=0.2,
            max_tokens=500,
            response_format={"type": "json_object"},
            # glm-4.6v-flash spends the whole output budget on reasoning by
            # default (finish_reason=length, empty content). Disabling it
            # leaves the budget for the JSON answer.
            extra_body={"thinking": {"type": "disabled"}},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": "Score this wallpaper and return JSON."},
                ]},
            ],
        )
        try:
            payload = json.loads(resp.choices[0].message.content)
        except (ValueError, TypeError, AttributeError, IndexError, KeyError) as exc:
            raise VisionError("provider returned malformed response") from exc
        try:
            tokens_in = int(resp.usage.prompt_tokens or 0)
            tokens_out = int(resp.usage.completion_tokens or 0)
            details = getattr(resp.usage, "completion_tokens_details", None)
            reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0)
        except (AttributeError, KeyError, TypeError, ValueError):
            tokens_in = tokens_out = reasoning_tokens = 0
        return VisionCallResult(
            scores=validate_scores(payload),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=int((time.monotonic() - started) * 1000),
            reasoning_tokens=reasoning_tokens,
        )
