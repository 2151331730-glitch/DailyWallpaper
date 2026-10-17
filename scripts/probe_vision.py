import argparse, json
from pathlib import Path
from dailywallpaper.analysis.vision import ZhipuVision

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--model", default="glm-4.6v-flash")
    args = parser.parse_args()
    import keyring
    key = keyring.get_password("DailyWallpaper", "zhipu")
    if not key:
        raise SystemExit("no key; run scripts/set_key.py first")
    result = ZhipuVision(key).analyze(args.image, args.model)
    print(json.dumps({
        "scores": result.scores.__dict__,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "latency_ms": result.latency_ms,
    }, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
