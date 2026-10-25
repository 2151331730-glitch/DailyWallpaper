# scripts/concept_report.py
"""Did the vision model actually learn what "sugar water" is?

Runs the app's own vision client over the labelled batch (cached, so a
second run is free), then answers three questions:

1. Does any score dimension separate sugar water from the rest? (AUC)
2. Which style/mood tags do? The model's tags are far richer than its
   scores -- 443 distinct combinations across 447 pictures -- so if the
   concept is learnable at all, it is probably in the tags.
3. Does the formulaic score track taste at all, or only "badly shot"?

    .venv\\Scripts\\python scripts\\concept_report.py --analyze   # costs ~¥0.2
    .venv\\Scripts\\python scripts\\concept_report.py             # report only
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# The console on this machine is GBK; the report uses ¥ and ✓. Without this
# the whole run dies at the last print, after the money has been spent.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import keyring  # noqa: E402

from dailywallpaper.analysis.images import canonicalize  # noqa: E402
from dailywallpaper.analysis.vision import (  # noqa: E402
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    VisionClient,
    ZhipuVision,
)
from dailywallpaper.config import load_config  # noqa: E402
from dailywallpaper.cost.budget import BudgetGuard  # noqa: E402
from dailywallpaper.db import connect, migrate, now_iso  # noqa: E402
from dailywallpaper.state.machine import SystemClock  # noqa: E402

BATCH_SOURCE = "concept_batch"
DIMENSIONS = ("quality", "creativity", "novelty", "formulaic")


# ------------------------------------------------------------------ stats ---

def auc(positives, negatives) -> float:
    """Probability a positive scores above a negative (ties count half).

    Implemented from ranks so a small batch needs no statistics library.
    """
    if not positives or not negatives:
        return float("nan")
    values = list(positives) + list(negatives)
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        rank = (start + end) / 2.0 + 1.0
        for position in range(start, end + 1):
            ranks[order[position]] = rank
        start = end + 1
    n_pos, n_neg = len(positives), len(negatives)
    rank_sum = sum(ranks[:n_pos])
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def spearman(xs, ys) -> float:
    pairs = [(x, y) for x, y in zip(xs, ys, strict=False) if x is not None]
    if len(pairs) < 3:
        return float("nan")
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        start = 0
        while start < len(order):
            end = start
            while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
                end += 1
            rank = (start + end) / 2.0 + 1.0
            for position in range(start, end + 1):
                out[order[position]] = rank
            start = end + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    left = sum((a - mx) ** 2 for a in rx) ** 0.5
    right = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (left * right) if left and right else float("nan")


# ------------------------------------------------------------------- data ---

def ensure_wallpaper(conn, item, thumb: Path) -> int:
    conn.execute(
        """insert into wallpapers(source,source_id,title,local_path,media_type,created_at)
           values(?,?,?,?,'image',?)
           on conflict(source,source_id) do update set local_path=excluded.local_path""",
        (BATCH_SOURCE, item["key"], item.get("title", ""), str(thumb), now_iso()),
    )
    conn.commit()
    return conn.execute(
        "select id from wallpapers where source=? and source_id=?",
        (BATCH_SOURCE, item["key"]),
    ).fetchone()[0]


def analyze(conn, cfg, batch_dir: Path | None = None) -> int:
    api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    if not api_key:
        print("凭据管理器里没有智谱 key，先跑 scripts/set_key.py")
        return 1
    clock = SystemClock()
    budget = BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, clock)
    client = VisionClient(
        provider=ZhipuVision(
            api_key,
            max_output_tokens=cfg.vision.max_output_tokens,
            temperature=cfg.vision.temperature,
        ),
        conn=conn,
        pricing=cfg.vision.pricing,
        budget=budget,
        max_output_tokens=cfg.vision.max_output_tokens,
    )
    batch = batch_dir or (ROOT / "data" / "concept_batch")
    meta = json.loads((batch / "meta.json").read_text(encoding="utf-8"))
    labels_path = batch / "labels.json"
    # A batch that has not been labelled yet still needs analysing; there the
    # whole batch is the target.
    labels = json.loads(labels_path.read_text(encoding="utf-8")) if labels_path.exists() else None
    normalized = batch / "normalized"

    done = 0
    for item in meta["items"]:
        if labels is not None and item["key"] not in labels:
            continue
        thumb = batch / item["thumb"].replace("\\", "/")
        if not thumb.exists():
            thumb = batch / "thumbs" / f"{item['key']}.jpg"
        if not thumb.exists():
            continue
        wallpaper_id = ensure_wallpaper(conn, item, thumb)
        canonical, image_hash = canonicalize(thumb, normalized)
        try:
            client.analyze_or_cache(
                wallpaper_id, image_hash, canonical, cfg.vision.screen_model
            )
        except Exception as exc:
            print(f"  {item['key']}: {type(exc).__name__} {exc}")
            continue
        done += 1
        if done % 20 == 0:
            print(f"  已完成 {done}")
    spent = conn.execute(
        "select coalesce(sum(cost_cny),0) from api_usage where ts like ?",
        (datetime.now().strftime("%Y-%m-%d") + "%",),
    ).fetchone()[0]
    print(f"分析完成 {done} 张；今天累计花费 CNY {spent:.3f}")
    return 0


def load_batch(conn, batch_dir: Path | None = None):
    batch = batch_dir or (ROOT / "data" / "concept_batch")
    meta = json.loads((batch / "meta.json").read_text(encoding="utf-8"))
    labels = json.loads((batch / "labels.json").read_text(encoding="utf-8"))
    rows = []
    for item in meta["items"]:
        label = labels.get(item["key"])
        if not label:
            continue
        record = None
        # Batches 1 and 3 were analysed under their own source; batch 2 reuses
        # pictures the daily pipeline had already analysed under the real one.
        for source, source_id in ((BATCH_SOURCE, item["key"]),
                                  (item.get("source"), item.get("source_id"))):
            if not source:
                continue
            record = conn.execute(
                """select a.quality,a.creativity,a.novelty,a.formulaic,
                          a.style_tags_json,a.mood_tags_json
                     from wallpapers w
                     join wallpaper_analyses wa on wa.wallpaper_id=w.id
                     join analyses a on a.id=wa.analysis_id
                    where w.source=? and w.source_id=?
                    order by a.id desc limit 1""",
                (source, str(source_id)),
            ).fetchone()
            if record is not None:
                break
        if record is None:
            continue
        rows.append({
            "key": item["key"], "origin": item["origin"],
            "looks": label["looks"], "sugar": label["sugar"],
            **{name: record[name] for name in DIMENSIONS},
            "style": json.loads(record["style_tags_json"] or "[]"),
            "mood": json.loads(record["mood_tags_json"] or "[]"),
        })
    return rows


# ----------------------------------------------------------------- report ---

def report(rows) -> str:
    # Wallpaper Engine items were judged from the project's own preview.jpg /
    # preview.gif or one frame of the video, never from the running wallpaper
    # (a scene.pkg cannot be rendered outside WE). The cover is the author's
    # pick, so those labels measure the cover, not the wallpaper: keep them out
    # of the metric and report them separately.
    main = [row for row in rows if row["origin"] != "we"]
    we_rows = [row for row in rows if row["origin"] == "we"]

    def sure_of(subset):
        return [row for row in subset if row["sugar"] in ("yes", "no")]

    sure = sure_of(main)
    pos = [row for row in sure if row["sugar"] == "yes"]
    neg = [row for row in sure if row["sugar"] == "no"]

    lines = [
        "# 糖水判别分析", "",
        f"生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}", "",
        "## 样本", "",
        f"- 已标注且已分析：**{len(rows)}** 张（其中 Wallhaven {len(main)}，WE {len(we_rows)}）",
        f"- 主分析只用 Wallhaven：明确判断 {len(sure)} 张（糖水 {len(pos)} / 不是 {len(neg)}），"
        f"说不好 {len(main) - len(sure)} 张不参与 AUC",
        "",
        "| 来源 | 张数 | 好看均值 | 糖水占比（明确判断内） |",
        "|---|---|---|---|",
    ]
    for origin, label in (("sugar", "Wallhaven 热门关键词"), ("random", "Wallhaven 随机"),
                          ("we", "Wallpaper Engine")):
        subset = [row for row in rows if row["origin"] == origin]
        judged = sure_of(subset)
        if not subset:
            continue
        rate = (sum(1 for row in judged if row["sugar"] == "yes") / len(judged)
                if judged else 0.0)
        lines.append(
            f"| {label} | {len(subset)} | "
            f"{statistics.fmean(row['looks'] for row in subset):.2f} | {rate:.0%} |"
        )

    lines += [
        "",
        "## 模型分数能不能区分糖水（Wallhaven）",
        "",
        "AUC = 随机取一张糖水、一张非糖水，糖水那张分数更高的概率。0.5 等于抛硬币。",
        "方向和大小要一起读：套路分理应是「糖水更高」（AUC > 0.5），",
        "创意分和新颖分理应是「糖水更低」（AUC < 0.5）。",
        "**区分度 = max(AUC, 1−AUC)**，衡量它到底有没有分开两类，与方向无关。",
        "",
        "| 分数维度 | 期望方向 | AUC | 区分度 | 判断 |",
        "|---|---|---|---|---|",
    ]
    expected = {"quality": None, "creativity": "lower", "novelty": "lower",
                "formulaic": "higher"}
    for name in DIMENSIONS:
        value = auc([row[name] for row in pos], [row[name] for row in neg])
        if value != value:
            continue
        separation = max(value, 1 - value)
        want = expected[name]
        direction = "任意"
        if want == "higher":
            direction = "糖水更高" + (" ✓" if value > 0.5 else " ✗")
        elif want == "lower":
            direction = "糖水更低" + (" ✓" if value < 0.5 else " ✗")
        verdict = ("有用" if separation >= 0.7 else
                   "弱" if separation >= 0.6 else "几乎没用")
        lines.append(f"| {name} | {direction} | {value:.3f} | {separation:.3f} | {verdict} |")

    lines += ["", "## 模型分数和「好不好看」的关系", "",
              "如果 formulaic 只跟难看相关，那它惩罚的是丑，不是套路。", "",
              "| 维度 | 与「好看」的 Spearman |", "|---|---|"]
    for name in DIMENSIONS:
        value = spearman([row[name] for row in main], [row["looks"] for row in main])
        if value == value:
            lines.append(f"| {name} | {value:+.3f} |")

    base = len(pos) / len(sure) if sure else 0.0
    stats = defaultdict(lambda: [0, 0])
    for row in sure:
        for tag in set(row["style"]) | set(row["mood"]):
            stats[tag][0] += 1
            stats[tag][1] += row["sugar"] == "yes"
    ranked = [
        (tag, count, hits / count)
        for tag, (count, hits) in stats.items() if count >= 6
    ]
    ranked.sort(key=lambda item: item[2], reverse=True)

    lines += ["", "## 哪些标签指向糖水（Wallhaven）", "",
              f"基准：明确判断的样本里 {base:.0%} 是糖水。出现 ≥6 次的标签：", "",
              "| 标签 | 出现次数 | 其中糖水占比 | 相对基准 |", "|---|---|---|---|"]
    for tag, count, rate in ranked[:10]:
        lines.append(f"| {tag} | {count} | {rate:.0%} | {rate / base:.2f}× |")
    lines += ["", "反向（最不像糖水）：", "",
              "| 标签 | 出现次数 | 其中糖水占比 | 相对基准 |", "|---|---|---|---|"]
    for tag, count, rate in ranked[-10:][::-1]:
        lines.append(f"| {tag} | {count} | {rate:.0%} | {rate / base:.2f}× |")

    we_judged = sure_of(we_rows)
    if we_rows:
        we_rate = (sum(1 for row in we_judged if row["sugar"] == "yes") / len(we_judged)
                   if we_judged else 0.0)
        lines += [
            "", "## Wallpaper Engine 单列（不参与上面的指标）", "",
            f"- {len(we_rows)} 张，明确判断 {len(we_judged)} 张，糖水占比 **{we_rate:.0%}**。",
            "- 这些标注看的是**封面**（preview.jpg / gif，或视频的第 2 秒一帧），"
            "不是实际运行的壁纸；scene.pkg 在 WE 之外根本渲染不出来。",
            "- 所以这个数说明的是「封面的套路程度」，方向可信，绝对值不可信。",
            "",
        ]

    lines += ["", "## 怎么用", "",
              "- AUC < 0.6 的维度不该再参与打分，它提供的是噪声。",
              "- 高相关的标签可以直接进 `avoid_tags`（现在的种子文件已经有这个机制）。",
              "- 样本只有一百多张，结论按方向读，别当定律。", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true", help="先跑视觉分析（会花钱）")
    parser.add_argument("--analyze-only", action="store_true", help="只分析，不出报告")
    parser.add_argument("--batch", type=Path, default=ROOT / "data" / "concept_batch")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "SUGARWATER.md")
    args = parser.parse_args(argv)

    conn = connect(ROOT / "data" / "app.db")
    migrate(conn)
    if args.analyze:
        code = analyze(conn, load_config(args.config), args.batch)
        if code:
            return code
        if args.analyze_only:
            return 0

    rows = load_batch(conn, args.batch)
    if len(rows) < 20:
        print(f"可用样本只有 {len(rows)} 张，先跑 --analyze")
        return 1
    text = report(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
