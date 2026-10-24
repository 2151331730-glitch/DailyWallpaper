# scripts/build_active_batch.py
"""Pick the next labelling batch by what the current model is unsure about.

Batch 1 was sampled by keywords, which tells us nothing about where the model
is weak. This one asks the trained sugar model for its opinion first and
takes the pictures it scores near the middle -- the ones that would teach it
the most -- plus a random control group.

The two groups are kept apart in meta.json on purpose:
- ``uncertain`` items are chosen *because* the model finds them hard, so any
  AUC measured on them is biased downward. Use them for training only.
- ``random`` items are an unbiased sample of the pool. Evaluate on those.

Everything here was already analysed, so the batch costs nothing and needs
no network.

    .venv\\Scripts\\python scripts\\build_active_batch.py
    .venv\\Scripts\\python scripts\\label_concept.py --batch data\\concept_batch2
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sqlite3
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dailywallpaper.recommend.sugar import SugarModel  # noqa: E402

THUMB = 640


def _rows(conn, source: str):
    return conn.execute(
        """select w.id, w.source, w.source_id, w.title, w.local_path,
                  a.style_tags_json, a.mood_tags_json
             from wallpapers w
             join wallpaper_analyses wa on wa.wallpaper_id = w.id
             join analyses a on a.id = wa.analysis_id
            where a.prompt_version = 'v3'
              and w.source = ?
              and w.local_path <> ''
              and not exists (select 1 from ratings r where r.wallpaper_id = w.id)
              and a.id = (select max(a2.id) from wallpaper_analyses wa2
                            join analyses a2 on a2.id = wa2.analysis_id
                           where wa2.wallpaper_id = w.id
                             and a2.prompt_version = 'v3')""",
        (source,),
    ).fetchall()


def _thumb(source: Path, dest: Path) -> bool:
    try:
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail((THUMB, THUMB))
            image.save(dest, "JPEG", quality=85)
        return True
    except Exception:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "concept_batch2")
    parser.add_argument("--uncertain", type=int, default=60)
    parser.add_argument("--random", type=int, default=100)
    parser.add_argument("--source", default="wallhaven")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--model", type=Path, default=ROOT / "data" / "sugar_model.json")
    args = parser.parse_args(argv)

    if not args.model.exists():
        print(f"先跑 scripts/train_sugar.py（找不到 {args.model}）")
        return 1
    model = SugarModel.load(args.model)

    conn = sqlite3.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True)
    rows = _rows(conn, args.source)
    if not rows:
        print("没有可用的未评分样本")
        return 1

    scored = []
    for row in rows:
        path = Path(row[4])
        if not path.exists():
            continue
        tags = json.loads(row[5] or "[]") + json.loads(row[6] or "[]")
        scored.append((model.score(tags), row, path))
    print(f"候选 {len(scored)} 张，模型分数分布："
          f"{min(s[0] for s in scored):.0f}–{max(s[0] for s in scored):.0f}")

    rng = random.Random(args.seed)
    uncertain = [item for item in scored if 45 <= item[0] <= 55]
    rng.shuffle(uncertain)
    chosen = [(score, row, path, "uncertain") for score, row, path in uncertain[:args.uncertain]]

    taken = {row[0] for _, row, _, _ in chosen}
    rest = [item for item in scored if item[1][0] not in taken]
    rng.shuffle(rest)
    chosen += [(score, row, path, "random") for score, row, path in rest[:args.random]]

    thumb_dir = args.out / "thumbs"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    items, skipped = [], 0
    for score, row, path, origin in chosen:
        key = f"wh-{row[2]}"
        dest = thumb_dir / f"{key}.jpg"
        if not dest.exists() and not _thumb(path, dest):
            skipped += 1
            continue
        items.append({
            "key": key, "source": row[1], "source_id": row[2],
            "title": row[3] or "", "origin": origin,
            "thumb": f"thumbs/{key}.jpg", "model_score": round(score, 1),
        })

    (args.out / "meta.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {name: sum(1 for item in items if item["origin"] == name)
              for name in ("uncertain", "random")}
    print(f"共 {len(items)} 张（拿不准 {counts['uncertain']} / 随机 {counts['random']}），"
          f"跳过 {skipped} 张 -> {args.out / 'meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
