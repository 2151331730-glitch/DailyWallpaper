"""Temporary diagnostic: print the raw vision response (never the key)."""

import base64
import time
from pathlib import Path

import keyring
from openai import OpenAI, RateLimitError

from dailywallpaper.analysis.prompts import SYSTEM_PROMPT
from dailywallpaper.analysis.vision import resolve_base_url
from dailywallpaper.config import load_config

PATH = r"D:\games\steam\steamapps\workshop\content\431960\1132505365\preview.jpg"


def main() -> int:
    cfg = load_config(Path("config.toml"))
    key = keyring.get_password("DailyWallpaper", "zhipu")
    client = OpenAI(
        api_key=key,
        base_url=resolve_base_url(cfg.vision.provider, cfg.vision.base_url),
    )
    with open(PATH, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    resp = None
    for attempt in range(1, 4):
        try:
            resp = client.chat.completions.create(
                model=cfg.vision.screen_model,
                temperature=0.2,
                max_tokens=cfg.vision.max_output_tokens,
                response_format={"type": "json_object"},
                extra_body=None if cfg.vision.thinking else
                {"thinking": {"type": "disabled"}},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                            {
                                "type": "text",
                                "text": "Score this wallpaper and return JSON.",
                            },
                        ],
                    },
                ],
            )
            break
        except RateLimitError as exc:
            print(f"attempt {attempt}: rate limited: {str(exc)[:200]}")
            if attempt == 3:
                return 1
            time.sleep(10 * attempt)

    if resp is None:
        return 1
    print("response_id:", resp.id)
    print("choices:", len(resp.choices))
    if resp.choices:
        choice = resp.choices[0]
        print("finish_reason:", choice.finish_reason)
        content = choice.message.content
        print("content_type:", type(content).__name__)
        print("content_repr:", repr(content)[:1200])
    print("usage:", resp.usage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
