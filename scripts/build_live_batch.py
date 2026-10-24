# scripts/build_live_batch.py
"""Fetch a fresh, unbiased batch that looks like tomorrow's candidates.

The previous batch came from the pictures the app had already fetched and
never chosen -- 78% sugar water, nothing like a normal day's pool. This one
asks the app's own Wallhaven source for a new list, so the mixture of sorts
(toplist / random / date_added) and the filters are exactly what the daily
pipeline sees.

Pictures already rated, or already in an earlier batch, are skipped: the
point is an independent test set.

    .venv\\Scripts\\python scripts/build_live_batch.py --limit 120
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dailywallpaper.app_support import configure_session  # noqa: E402
from dailywallpaper.config import load_config  # noqa: E402
from dailywallpaper.sources.wallhaven import WallhavenSource  # noqa: E402

THUMB = 640


def _known_ids(conn) -> set[str]:
    """Anything already rated, or already labelled in an earlier batch."""
    ids = {str(row[0]) for row in conn.execute(
        "select w.source_id from wallpapers w join ratings r on r.wallpaper_id=w.id")}
    ids |= {str(row[0]) for row in conn.execute(
        "select source_id from wallpapers where source='concept_batch'")}
    return ids


def _save(source_bytes: bytes, dest: Path) -> bool:
    try:
        dest.write_bytes(source_bytes)
        with Image.open(dest) as image:
            image = image.convert("RGB")
            image.thumbnail((THUMB, THUMB))
            image.save(dest, "JPEG", quality=85)
        return True
    except Exception:
        dest.unlink(missing_ok=True)
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=120)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "concept_batch3")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--pages", type=int, default=8)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    session: requests.Session = configure_session(cfg)
    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True)
    known = _known_ids(conn)

    candidates = WallhavenSource(session, cfg.sources).fetch(args.limit * 2, max_pages=args.pages)
    thumb_dir = args.out / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)

    items, skipped = [], 0
    for candidate in candidates:
        if len(items) >= args.limit:
            break
        if str(candidate.source_id) in known:
            continue
        key = f"wh-{candidate.source_id}"
        dest = thumb_dir / f"{key}.jpg"
        if not dest.exists():
            if not candidate.preview_url:
                skipped += 1
                continue
            try:
                response = session.get(candidate.preview_url, timeout=20)
                response.raise_for_status()
            except Exception:
                skipped += 1
                continue
            if not _save(response.content, dest):
                skipped += 1
                continue
        items.append({
            "key": key, "source": candidate.source,
            "source_id": candidate.source_id, "title": candidate.title or "",
            "origin": "live", "thumb": f"thumbs/{key}.jpg",
        })

    (args.out / "meta.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"新批次 {len(items)} 张（跳过 {skipped}）-> {args.out / 'meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
