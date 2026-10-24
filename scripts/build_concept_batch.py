# scripts/build_concept_batch.py
"""Build a labelling batch for "sugar water" -- pretty, empty, template-like.

Kept deliberately OUTSIDE ``wallpapers`` / ``ratings``: this batch is a
teaching set, half of it hand-picked to be sugar water. Mixing it into the
ratings would skew the evaluation set, which must stay representative of
what the app really sees every day.

Composition:
- sugar-prone: Wallhaven's popular lists for the usual suspects (neon,
  cyberpunk, starry sky, generic fantasy landscape, anime ...)
- random: Wallhaven ``sorting=random``, the control group
- we: local Wallpaper Engine workshop items, thumbnailed from preview.jpg,
  preview.gif or a frame of their video

    .venv\\Scripts\\python scripts\\build_concept_batch.py
    .venv\\Scripts\\python scripts\\build_concept_batch.py --sugar 40 --random 30
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

API = "https://wallhaven.cc/api/v1/search"
# The keywords people mean when they say "糖水": polished, familiar, no idea.
SUGAR_QUERIES = [
    "cyberpunk", "neon", "fantasy landscape", "starry sky", "mountain",
    "anime", "abstract", "space", "sci-fi", "dragon",
]
FILTERS = {"purity": "100", "atleast": "1920x1080", "ratios": "16x9"}


def _session(proxy: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers["User-Agent"] = "DailyWallpaper-concept-batch/1.0"
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def _wallhaven(session, params: dict, timeout=15) -> list[dict]:
    try:
        response = session.get(API, params=params, timeout=timeout)
        response.raise_for_status()
    except Exception as exc:  # a dead source must not end the batch
        print(f"  wallhaven {params}: {type(exc).__name__}")
        return []
    return response.json().get("data", [])


def sugar_items(session, per_query: int) -> list[dict]:
    items = []
    for query in SUGAR_QUERIES:
        params = {**FILTERS, "sorting": "toplist", "topRange": "1M",
                  "categories": "010" if query == "anime" else "111",
                  "q": query}
        items += _wallhaven(session, params)[:per_query]
        print(f"  sugar {query}: {len(items)} so far")
    return items


def random_items(session, count: int) -> list[dict]:
    items = []
    while len(items) < count:
        params = {**FILTERS, "sorting": "random", "categories": "111"}
        batch = _wallhaven(session, params)
        if not batch:
            break
        items += batch
    return items[:count]


def _thumb_from_wallhaven(session, item: dict, dest: Path) -> bool:
    url = (item.get("thumbs") or {}).get("large") or item.get("path")
    if not url:
        return False
    try:
        response = session.get(url, timeout=20)
        response.raise_for_status()
        dest.write_bytes(response.content)
        return True
    except Exception:
        return False


def _first_frame(video: Path, dest: Path) -> bool:
    """A video wallpaper has no preview; one frame is enough to judge it."""
    ffmpeg = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
    command = [str(ffmpeg) if ffmpeg.exists() else "ffmpeg", "-y", "-loglevel", "error",
               "-ss", "2", "-i", str(video), "-frames:v", "1", str(dest)]
    try:
        subprocess.run(command, check=True, timeout=60, capture_output=True)
        return dest.exists()
    except Exception:
        return False


def _save_as_jpg(source: Path, dest: Path, size=640) -> bool:
    try:
        image = Image.open(source)
        image.seek(0)  # animated gif: first frame
        image = image.convert("RGB")
        image.thumbnail((size, size))
        image.save(dest, "JPEG", quality=85)
        return True
    except Exception:
        return False


def we_items(workshop: Path, dest_dir: Path) -> list[dict]:
    items = []
    for project in sorted(path for path in workshop.iterdir() if path.is_dir()):
        if project.name.startswith("_"):
            continue
        dest = dest_dir / f"we-{project.name}.jpg"
        made = False
        for name in ("preview.jpg", "preview.png", "preview.gif"):
            source = project / name
            if source.exists() and _save_as_jpg(source, dest):
                made = True
                break
        if not made:
            videos = [p for p in project.iterdir()
                      if p.suffix.lower() in (".mp4", ".webm")]
            made = bool(videos) and _first_frame(videos[0], dest) and _save_as_jpg(dest, dest)
        if not made:
            continue
        title = project.name
        try:
            meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
            title = meta.get("title") or title
        except Exception:
            pass
        items.append({"key": f"we-{project.name}", "source": "we",
                      "source_id": project.name, "title": title,
                      "origin": "we", "thumb": str(dest.relative_to(dest_dir.parent))})
    return items


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sugar", type=int, default=9, help="per query")
    parser.add_argument("--random", type=int, default=60)
    parser.add_argument("--we", type=int, default=25)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "concept_batch")
    parser.add_argument("--proxy", default="http://127.0.0.1:7892")
    args = parser.parse_args(argv)

    thumb_dir = args.out / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    session = _session(args.proxy)

    print("抓 Wallhaven（糖水倾向）...")
    sugar = sugar_items(session, args.sugar)
    print("抓 Wallhaven（随机对照）...")
    random = random_items(session, args.random)

    items, seen = [], set()
    for entry, origin in [(item, "sugar") for item in sugar] + [
            (item, "random") for item in random]:
        key = f"wh-{entry['id']}"
        if key in seen:
            continue
        seen.add(key)
        dest = thumb_dir / f"{key}.jpg"
        if not dest.exists() and not _thumb_from_wallhaven(session, entry, dest):
            continue
        items.append({"key": key, "source": "wallhaven", "source_id": entry["id"],
                      "title": entry.get("id", ""), "origin": origin,
                      "thumb": str(dest.relative_to(args.out))})
    print(f"  wallhaven 可用 {len(items)} 张")

    workshop = ROOT / "data" / "steam_workshop"
    we = we_items(workshop, thumb_dir)[:args.we] if workshop.exists() else []
    items += we
    print(f"  wallpaper engine 可用 {len(we)} 张")

    (args.out / "meta.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n共 {len(items)} 张 -> {args.out / 'meta.json'}")
    print(f"糖水倾向 {sum(1 for i in items if i['origin'] == 'sugar')} / "
          f"随机 {sum(1 for i in items if i['origin'] == 'random')} / "
          f"WE {sum(1 for i in items if i['origin'] == 'we')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
