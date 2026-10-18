"""Phase 0 probe: run one real vision call and print the JSON result.

Retries on provider rate limiting (HTTP 429, Zhipu code 1305) with
backoff. This retry lives in the probe, not in the product client --
retry and budget policy belong to Task 1.4's VisionClient.
"""

import argparse
import json
import time
from pathlib import Path

from openai import RateLimitError

from dailywallpaper.analysis.vision import ZhipuVision

BACKOFF_SECONDS = [30, 60, 120, 180]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--model", default="glm-4.6v-flash")
    args = parser.parse_args()

    import keyring

    key = keyring.get_password("DailyWallpaper", "zhipu")
    if not key:
        raise SystemExit("no key; run scripts/set_key.py first")

    provider = ZhipuVision(key)
    for attempt in range(1, len(BACKOFF_SECONDS) + 2):
        try:
            result = provider.analyze(args.image, args.model)
            break
        except RateLimitError as exc:
            if attempt > len(BACKOFF_SECONDS):
                raise
            wait = BACKOFF_SECONDS[attempt - 1]
            print(
                f"attempt {attempt}: rate limited ({str(exc)[:120]}), retrying in {wait}s",
                flush=True,
            )
            time.sleep(wait)

    print(
        json.dumps(
            {
                "model": args.model,
                "scores": result.scores.__dict__,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "reasoning_tokens": result.reasoning_tokens,
                "latency_ms": result.latency_ms,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
