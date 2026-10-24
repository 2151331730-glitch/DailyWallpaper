# scripts/evaluate_mixed.py
"""Should static and dynamic candidates be ranked in one pool?

The only honest way to answer is cross-type pairs: take a rated static
picture and a rated dynamic one, ask which the user liked more, and see
whether the scores agree. If contact sheets are systematically marked down
(scripts/analyze_dynamic.py measured -7.5 creativity on video), the
uncalibrated ranking will lose those pairs and the calibrated one should win
them back.

    .venv\\Scripts\\python scripts\\evaluate_mixed.py
"""
from __future__ import annotations

import json
import statistics
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dailywallpaper.analysis.scoring import (  # noqa: E402
    EXPLORE_WEIGHT,
    PERSONALIZE_WEIGHT,
    explore_score,
    personalize_score,
)
from dailywallpaper.db import connect, migrate  # noqa: E402
from dailywallpaper.recommend.profile import PreferenceProfile  # noqa: E402

DIMENSIONS = ("quality", "creativity", "novelty", "formulaic")
# Measured on 24 dynamic wallpapers against a re-encoded noise floor.
CALIBRATION = {"creativity": 5.8, "novelty": 3.1, "formulaic": -2.1}
DYNAMIC = ROOT / "data" / "concept_batch_dynamic"


def _row_dict(record):
    tags = json.loads(record["style_tags_json"] or "[]")
    mood = json.loads(record["mood_tags_json"] or "[]")
    return {"style": tags, "mood": mood,
            **{name: record[name] for name in DIMENSIONS}}


def _static(conn):
    rows = conn.execute(
        """select r.rating, a.quality, a.creativity, a.novelty, a.formulaic,
                  a.style_tags_json, a.mood_tags_json, w.source
             from ratings r
             join wallpapers w on w.id = r.wallpaper_id
             join wallpaper_analyses wa on wa.wallpaper_id = r.wallpaper_id
             join analyses a on a.id = wa.analysis_id
            where a.prompt_version = 'v3'""").fetchall()
    out = []
    for record in rows:
        item = _row_dict(record)
        item.update(rating=int(record["rating"]), source=record["source"], kind="image")
        out.append(item)
    return out


def _dynamic(conn):
    meta = json.loads((DYNAMIC / "meta.json").read_text(encoding="utf-8"))
    labels = json.loads((DYNAMIC / "labels.json").read_text(encoding="utf-8"))
    out = []
    for item in meta["items"]:
        label = labels.get(item["key"])
        if not label:
            continue
        record = conn.execute(
            """select a.quality,a.creativity,a.novelty,a.formulaic,
                      a.style_tags_json,a.mood_tags_json
                 from wallpapers w
                 join wallpaper_analyses wa on wa.wallpaper_id=w.id
                 join analyses a on a.id=wa.analysis_id
                where w.source='concept_batch' and w.source_id=?
                order by a.id desc limit 1""", (item["key"],)).fetchone()
        if record is None:
            continue
        row = _row_dict(record)
        row.update(rating=int(label["looks"]), source="we", kind=item["kind"])
        out.append(row)
    return out


def _blend(row, profile, calibrate: bool) -> float:
    if calibrate and row["kind"] in ("video", "gif"):
        row = dict(row)
        for name, delta in CALIBRATION.items():
            row[name] = row[name] + delta
    from dailywallpaper.analysis.vision import VisionScores
    scores = VisionScores(
        quality=int(row["quality"]), creativity=int(row["creativity"]),
        novelty=int(row["novelty"]), formulaic=int(row["formulaic"]),
        nsfw=False, watermark=False, text_overlay="none",
        style_tags=row["style"], mood_tags=row["mood"], palette=[],
        subject="", reason="")
    explore = explore_score(scores, row["source"], ())
    raw = personalize_score(scores, profile, row["source"], 0)
    count = profile.rating_count
    from dailywallpaper.analysis.scoring import COLD_START_PRIOR, PRIOR_STRENGTH
    personalize = ((count / (count + PRIOR_STRENGTH)) * raw
                   + (PRIOR_STRENGTH / (count + PRIOR_STRENGTH)) * COLD_START_PRIOR)
    return EXPLORE_WEIGHT * explore + PERSONALIZE_WEIGHT * personalize


def cross_type_accuracy(static, dynamic, profile, calibrate: bool):
    """Over every static/dynamic pair, does the score order follow the rating?"""
    scored_static = [(row, _blend(row, profile, False)) for row in static]
    scored_dynamic = [(row, _blend(row, profile, calibrate)) for row in dynamic]
    correct = ties = total = 0
    for left, left_score in scored_static:
        for right, right_score in scored_dynamic:
            if abs(left["rating"] - right["rating"]) < 1:
                continue
            total += 1
            if left_score == right_score:
                ties += 1
                continue
            better_left = left["rating"] > right["rating"]
            if (left_score > right_score) == better_left:
                correct += 1
    return ((correct + 0.5 * ties) / total if total else 0.0), total


def main() -> int:
    conn = connect(ROOT / "data" / "app.db")
    migrate(conn)
    static = _static(conn)
    dynamic = _dynamic(conn)
    if not static or not dynamic:
        print(f"样本不足：static {len(static)} / dynamic {len(dynamic)}")
        return 1
    profile = PreferenceProfile.from_ratings(
        [{"rating": row["rating"], "created_at": datetime.now().astimezone().isoformat(),
          "style_tags_json": json.dumps(row["style"]),
          "mood_tags_json": json.dumps(row["mood"]), "source": row["source"]}
         for row in static], datetime.now().astimezone())

    raw, total = cross_type_accuracy(static, dynamic, profile, calibrate=False)
    cal, _ = cross_type_accuracy(static, dynamic, profile, calibrate=True)
    print(f"静态 {len(static)} 张 / 动态 {len(dynamic)} 个，跨类型配对数 {total}")
    print(f"未校准 跨类型准确率: {raw:.3f}")
    print(f"已校准 跨类型准确率: {cal:.3f}")
    print(f"校准带来的变化: {cal - raw:+.3f}")
    print(f"\n动态组人类评分均值 {statistics.fmean(r['rating'] for r in dynamic):.2f}，"
          f"静态组 {statistics.fmean(r['rating'] for r in static):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
