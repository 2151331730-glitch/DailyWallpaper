# scripts/evaluate.py
"""Offline evaluation: does the ranking agree with the human ratings?

One question only -- when the recommender sorts a pool the user has already
rated, does its order match the user's? Everything comes from cached
analyses, so a run costs nothing and never touches the desktop.

    .venv\\Scripts\\python scripts\\evaluate.py            # writes docs/EVALUATION.md
    .venv\\Scripts\\python scripts\\evaluate.py --help

Method (and why):
- The ratings are split by score, 70/30. The profile is built from the 70%
  and the metrics are measured on the 30%: evaluating on the same ratings
  that taught the profile would just measure how well it memorised them.
- NDCG@5 needs several queries, so the test split is dealt into pools of
  about one day's candidate count. A single 150-item ranking would be one
  noisy query; forty-item pools look like the decision the app really faces.
- Random is the floor. If a variant cannot beat it, it is not doing anything.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dailywallpaper.analysis.scoring import (  # noqa: E402
    COLD_START_PRIOR,
    EXPLORE_WEIGHT,
    PERSONALIZE_WEIGHT,
    PRIOR_STRENGTH,
    explore_score,
    final_score,
    personalize_score,
)
from dailywallpaper.analysis.vision import VisionScores  # noqa: E402
from dailywallpaper.recommend.profile import PreferenceProfile  # noqa: E402

PROMPT_VERSION = "v3"
POOL_SIZE = 40
TOP_K = 5
TEST_RATIO = 0.3
SEED = 7
RANDOM_TRIALS = 20


# ---------------------------------------------------------------- metrics ---

def _discount(position: int) -> float:
    """1-based position -> weight. The first result counts fully."""
    return 1.0 / math.log2(position + 1)


def dcg(gains) -> float:
    return sum(gain * _discount(position)
               for position, gain in enumerate(gains, start=1))


def ndcg_at_k(ranked, ideal, k: int) -> float:
    """Discounted gain of the first k, over the best any order could get.

    ``ideal`` is the same items' relevance already sorted best-first.
    """
    ceiling = dcg(list(ideal)[:k])
    if ceiling <= 0:
        return 0.0
    return dcg(list(ranked)[:k]) / ceiling


def _ranks(values) -> list[float]:
    """Competition ranks with ties averaged, so ties cannot fake a correlation."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while (end + 1 < len(order)
               and values[order[end + 1]] == values[order[start]]):
            end += 1
        rank = (start + end) / 2.0 + 1.0
        for position in range(start, end + 1):
            ranks[order[position]] = rank
        start = end + 1
    return ranks


def spearman(xs, ys) -> float:
    """Rank correlation. 1 is the same order, -1 the exact opposite."""
    xs, ys = list(xs), list(ys)
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx, ry = _ranks(xs), _ranks(ys)
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    numerator = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    left = math.sqrt(sum((a - mx) ** 2 for a in rx))
    right = math.sqrt(sum((b - my) ** 2 for b in ry))
    if left == 0 or right == 0:
        return 0.0
    return numerator / (left * right)


# ------------------------------------------------------------ the split ---

def split_rows(rows, test_ratio: float = TEST_RATIO, seed: int = SEED):
    """Stratified by rating: every score keeps its share in both halves.

    A plain shuffle would happily put all five 10s in the test half or none,
    and the metric would swing on that luck rather than on the ranking.
    """
    by_rating = defaultdict(list)
    for row in rows:
        by_rating[row["rating"]].append(row)
    rng = random.Random(seed)
    train: list = []
    test: list = []
    for rating in sorted(by_rating):
        group = list(by_rating[rating])
        rng.shuffle(group)
        cut = int(round(len(group) * test_ratio))
        test.extend(group[:cut])
        train.extend(group[cut:])
    return train, test


def make_pools(items, size: int = POOL_SIZE, seed: int = SEED, min_size=None):
    """Deal the test items into pseudo-days; a short remainder is dropped."""
    floor = size if min_size is None else min_size
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    pools = []
    for start in range(0, len(shuffled), size):
        chunk = shuffled[start:start + size]
        if len(chunk) >= floor:
            pools.append(chunk)
    return pools


def relevance(rows) -> list[int]:
    return [int(row["rating"]) for row in rows]


def pairwise_accuracy(scores, ratings, min_gap: int = 1):
    """How often the higher-scored picture is also the better-loved one.

    Every pair whose ratings differ by at least ``min_gap`` is one question.
    NDCG@5 over a single pool answers one question per run; this answers
    hundreds, which is what makes a spread meaningful. 0.5 is a coin.
    """
    scores, ratings = list(scores), list(ratings)
    correct = ties = pairs = 0
    for left in range(len(ratings)):
        for right in range(left + 1, len(ratings)):
            if abs(ratings[left] - ratings[right]) < min_gap:
                continue
            pairs += 1
            if scores[left] == scores[right]:
                ties += 1
                continue
            better = left if ratings[left] > ratings[right] else right
            if (scores[left] > scores[right]) == (better == left):
                correct += 1
    if not pairs:
        return 0.0, 0
    return (correct + 0.5 * ties) / pairs, pairs


def kfold_split(rows, k: int = 5, seed: int = SEED):
    """Stratified k folds: every rating keeps its share in every fold."""
    by_rating = defaultdict(list)
    for row in rows:
        by_rating[row["rating"]].append(row)
    rng = random.Random(seed)
    folds: list[list] = [[] for _ in range(k)]
    for rating in sorted(by_rating):
        group = list(by_rating[rating])
        rng.shuffle(group)
        offset = rng.randrange(k)
        for index, row in enumerate(group):
            folds[(offset + index) % k].append(row)
    return [
        ([row for other, fold in enumerate(folds) if other != index for row in fold],
         folds[index])
        for index in range(k)
    ]


def cross_validate(rows, k: int = 5, seed: int = SEED, min_gap: int = 1) -> dict:
    """Pairwise accuracy per strategy, averaged over the folds.

    The profile is rebuilt from the training folds every time, so a picture
    that taught the profile never appears in the folds that judge it.
    """
    names = ("explore", "personalize", "blend", "weighted")
    per_fold = {name: [] for name in names}
    pairs_total = 0
    for train, test in kfold_split(rows, k, seed):
        profile = PreferenceProfile.from_ratings(train, datetime.now().astimezone())
        scored = [
            (score_variants(_scores_from_row(row), row["source"], profile), row["rating"])
            for row in test
        ]
        for name in names:
            accuracy, pairs = pairwise_accuracy(
                [scores[name] for scores, _ in scored],
                [rating for _, rating in scored], min_gap)
            per_fold[name].append(accuracy)
            if name == "weighted":
                pairs_total += pairs
    summary = {
        name: {
            "accuracy_mean": statistics.fmean(values),
            "accuracy_sd": statistics.pstdev(values) if len(values) > 1 else 0.0,
        }
        for name, values in per_fold.items()
    }
    summary["pairs"] = pairs_total
    summary["folds"] = k
    return summary


# --------------------------------------------------------------- scoring ---

def _scores_from_row(row) -> VisionScores:
    extra = _json(row["scores_json"], {})
    return VisionScores(
        quality=int(row["quality"]),
        creativity=int(row["creativity"]),
        novelty=int(row["novelty"]),
        formulaic=int(row["formulaic"]),
        nsfw=bool(row["nsfw"]),
        watermark=bool(extra.get("watermark", False)),
        text_overlay=str(extra.get("text_overlay", "") or ""),
        style_tags=_json(row["style_tags_json"], []),
        mood_tags=_json(row["mood_tags_json"], []),
        palette=_json(row["palette_json"], []),
        subject=str(extra.get("subject", "")),
        reason=str(extra.get("reason", "")),
    )


def _json(raw, fallback):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def score_variants(scores: VisionScores, source: str, profile,
                   weather_code: int = 0) -> dict[str, float]:
    """The three things worth comparing, on exactly the same picture.

    ``personalize`` carries the same cold-start smoothing the weighted score
    uses; without it a profile of a few ratings would look artificially good.
    """
    explore = explore_score(scores, source, ())
    raw = personalize_score(scores, profile, source, weather_code)
    count = profile.rating_count
    personalize = (
        (count / (count + PRIOR_STRENGTH)) * raw
        + (PRIOR_STRENGTH / (count + PRIOR_STRENGTH)) * COLD_START_PRIOR
        if count
        else COLD_START_PRIOR
    )
    full = final_score(scores, source, profile, (), weather_code)
    # The plain mix without the formulaic penalty, so a run can tell whether a
    # drop comes from the 55/45 weighting or from the penalty stacked on top.
    blend = EXPLORE_WEIGHT * explore + PERSONALIZE_WEIGHT * personalize
    return {
        "explore": explore,
        "personalize": personalize,
        "blend": blend,
        "weighted": full.final,
    }


def _ordered(rows, scores_by_id, key: str):
    return sorted(rows, key=lambda row: -scores_by_id[row[key]])


# ------------------------------------------------------------------ data ---

def load_rows(conn):
    """Rated wallpapers that have a current-version analysis."""
    sql = """
        select r.wallpaper_id, r.rating, r.created_at,
               a.quality, a.creativity, a.novelty, a.formulaic, a.nsfw,
               a.scores_json, a.style_tags_json, a.mood_tags_json, a.palette_json,
               w.source
          from ratings r
          join wallpapers w on w.id = r.wallpaper_id
          join wallpaper_analyses wa on wa.wallpaper_id = r.wallpaper_id
          join analyses a on a.id = wa.analysis_id
         where a.prompt_version = ?
           and a.id = (
                 select max(a2.id) from wallpaper_analyses wa2
                   join analyses a2 on a2.id = wa2.analysis_id
                  where wa2.wallpaper_id = r.wallpaper_id
                    and a2.prompt_version = ?)
    """
    return conn.execute(sql, (PROMPT_VERSION, PROMPT_VERSION)).fetchall()


def count_ratings(conn) -> int:
    return conn.execute("select count(distinct wallpaper_id) from ratings").fetchone()[0]


def data_quality(rows) -> dict:
    """Identical four-score vectors mean the model stopped telling pictures apart."""
    vectors = defaultdict(list)
    for row in rows:
        vectors[(row["quality"], row["creativity"], row["novelty"],
                 row["formulaic"])].append(row["wallpaper_id"])
    groups = [ids for ids in vectors.values() if len(ids) > 1]
    ratings = defaultdict(int)
    for row in rows:
        ratings[int(row["rating"])] += 1
    return {
        "duplicate_groups": len(groups),
        "largest_duplicate": max((len(ids) for ids in groups), default=1),
        "duplicate_items": sum(len(ids) for ids in groups),
        "rating_histogram": dict(sorted(ratings.items())),
    }


# ------------------------------------------------------------ evaluation ---

def evaluate(test_rows, profile, seed: int = SEED, pool_size: int = POOL_SIZE,
             top_k: int = TOP_K) -> dict:
    """NDCG@5 per pseudo-pool, averaged; plus a whole-split Spearman."""
    scored = []
    for row in test_rows:
        scores = _scores_from_row(row)
        variants = score_variants(scores, row["source"], profile)
        scored.append((dict(row), variants))

    pools = make_pools(scored, size=pool_size, seed=seed)
    names = ("explore", "personalize", "blend", "weighted")
    per_pool = {name: [] for name in (*names, "random")}
    all_scores = {name: [] for name in names}
    all_ratings = []

    for pool in pools:
        ideal = sorted((item[0]["rating"] for item in pool), reverse=True)
        for name in names:
            values = {item[0]["wallpaper_id"]: item[1][name] for item in pool}
            order = sorted(pool, key=lambda item: -values[item[0]["wallpaper_id"]])
            per_pool[name].append(ndcg_at_k(relevance([i[0] for i in order]), ideal, top_k))
        rng = random.Random(seed)
        trials = []
        for _ in range(RANDOM_TRIALS):
            order = list(pool)
            rng.shuffle(order)
            trials.append(ndcg_at_k(relevance([i[0] for i in order]), ideal, top_k))
        per_pool["random"].append(statistics.fmean(trials))

    for item in scored:
        real = item[0]["rating"]
        all_ratings.append(real)
        for name in names:
            all_scores[name].append(item[1][name])

    summary = {}
    for name, values in per_pool.items():
        summary[name] = {
            "ndcg_mean": statistics.fmean(values) if values else 0.0,
            "ndcg_sd": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "ndcg_min": min(values) if values else 0.0,
            "ndcg_max": max(values) if values else 0.0,
            "pools": len(values),
        }
    for name in names:
        summary[name]["spearman"] = spearman(all_scores[name], all_ratings)
    summary["random"]["spearman"] = 0.0
    return summary


def history_check(conn, profile) -> dict | None:
    """The wallpapers the app actually applied and the user rated (small n)."""
    sql = """
        select r.wallpaper_id, r.rating,
               a.quality, a.creativity, a.novelty, a.formulaic, a.nsfw,
               a.scores_json, a.style_tags_json, a.mood_tags_json, a.palette_json,
               w.source
          from ratings r
          join wallpapers w on w.id = r.wallpaper_id
          join wallpaper_analyses wa on wa.wallpaper_id = r.wallpaper_id
          join analyses a on a.id = wa.analysis_id
         where a.prompt_version = ?
           and exists (select 1 from decisions d
                        where d.wallpaper_id = r.wallpaper_id and d.status = 'applied')
    """
    rows = conn.execute(sql, (PROMPT_VERSION,)).fetchall()
    if len(rows) < 5:
        return None
    predicted = [score_variants(_scores_from_row(row), row["source"], profile)["weighted"]
                 for row in rows]
    return {"n": len(rows), "spearman": spearman(predicted, relevance(rows))}


# ---------------------------------------------------------------- report ---

def _fmt(value: float) -> str:
    return f"{value:.3f}"


def render_report(summary, quality, history, stats, cv) -> str:
    strategy_labels = {
        "explore": "纯探索",
        "personalize": "纯个性化",
        "blend": "55/45 纯混合",
        "weighted": "55/45 + 惩罚（现状）",
    }
    best_label = strategy_labels[
        max(strategy_labels, key=lambda name: cv[name]["accuracy_mean"])
    ]
    lines = [
        "# 离线评估报告",
        "",
        f"生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}",
        "",
        "由 `scripts/evaluate.py` 自动生成，数据全部来自 `data/app.db` 的缓存分析，",
        "不联网、不调用模型、不改动桌面。",
        "",
        "## 数据",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 评分总数 | {stats['ratings']} |",
        f"| 有 {PROMPT_VERSION} 分析、可参与评估 | {stats['usable']} |",
        f"| 训练集（建画像） | {stats['train']} |",
        f"| 测试集（只评估） | {stats['test']} |",
        f"| 伪候选池 | {stats['pools']} 个 × {stats['pool_size']} 张 |",
        "",
        "## 数据质量",
        "",
        f"- 四维分数完全相同的组：**{quality['duplicate_groups']}** 组，"
        f"涉及 {quality['duplicate_items']} 张图，最大一组 {quality['largest_duplicate']} 张。",
        "- 这项越大，模型分的信息量越少（不同图片拿到同一组分数，排序只能靠掷骰子）。",
        f"- 评分分布：{quality['rating_histogram']}",
        "",
        "## 成对比较（主结论）",
        "",
        f"{cv['folds']} 折交叉验证：每折 80% 建画像、20% 只评估；"
        "每折里所有评分差 ≥1 的两两配对各问一次，"
        f"共 {cv['pairs']} 个问题。",
        "",
        "准确率 = 「分数更高的那张，确实是你打分更高的那张」的比例。0.5 等于抛硬币。",
        "标准差是折与折之间的波动，不是抽样误差。",
        "",
        "| 排序策略 | 成对准确率 | 折间标准差 |",
        "|---|---|---|",
        *[
            f"| {strategy_labels[name]} | {cv[name]['accuracy_mean']:.3f} "
            f"(±{cv[name]['accuracy_sd']:.3f}) |"
            for name in ("explore", "personalize", "blend", "weighted")
        ],
        "",
        f"最好的策略：**{best_label}**",
        "",
        "## NDCG@5（单次切分，仅参考）",
        "",
        f"NDCG@{TOP_K} 按伪候选池计算后取平均，括号内为标准差；"
        "Spearman 在整个测试集上计算。测试集只有几十张时只会切出一个池，"
        "标准差因此恒为 0——这正是改用成对比较的原因。",
        "",
        "| 排序策略 | NDCG@5 | Spearman |",
        "|---|---|---|",
    ]
    labels = {
        "random": "随机（下限）",
        "explore": "纯探索",
        "personalize": "纯个性化",
        "blend": "55/45 纯混合",
        "weighted": "55/45 + 惩罚（现状）",
    }
    order = ("random", "explore", "personalize", "blend", "weighted")
    for name in order:
        row = summary[name]
        lines.append(
            f"| {labels[name]} | {_fmt(row['ndcg_mean'])} "
            f"(±{_fmt(row['ndcg_sd'])}) | {_fmt(row['spearman'])} |"
        )
    scored_names = ("explore", "personalize", "blend", "weighted")
    best = max(scored_names, key=lambda name: summary[name]["ndcg_mean"])
    lines += [
        "",
        f"最好的策略：**{labels[best]}**",
        "",
    ]
    if history:
        lines += [
            "## 真实历史（小样本）",
            "",
            f"程序实际应用过、且你评过分的壁纸：**n = {history['n']}**，"
            f"模型分与你的评分的 Spearman = **{_fmt(history['spearman'])}**。",
            "",
            "样本量太小，这个数只能当方向参考，不能下结论。攒到 100 条以上才有意义。",
            "",
        ]
    else:
        lines += ["## 真实历史（小样本）", "",
                  "实际应用过且有评分的壁纸不足 5 条，暂无法计算。", ""]
    lines += [
        "## 局限",
        "",
        "- 评估集是 Wallhaven 的策展样本（占 98%），结论主要说明静态图的审美排序，",
        "  不代表 Steam 动态壁纸。",
        "- 离线回放测的是排序逻辑；它回答不了“换个来源能不能找到更好的图”。",
        "- 训练/测试按评分分层切分，同一张图不会同时出现在两侧；但同一位作者的同系列作品",
        "  可能被分到两侧，会略微高估。",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "app.db")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "EVALUATION.md")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--pool-size", type=int, default=POOL_SIZE)
    parser.add_argument("--test-ratio", type=float, default=TEST_RATIO)
    parser.add_argument("--folds", type=int, default=5)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"找不到数据库：{args.db}")
        return 1
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = [dict(row) for row in load_rows(conn)]
    if len(rows) < 20:
        print(f"可评估的评分只有 {len(rows)} 条，至少需要 20 条（先跑 --curate 打分）。")
        return 1
    stats = {"ratings": count_ratings(conn), "usable": len(rows)}
    train, test = split_rows(rows, args.test_ratio, args.seed)
    stats.update(train=len(train), test=len(test),
                 pool_size=args.pool_size,
                 pools=len(make_pools(test, args.pool_size, args.seed)))

    profile = PreferenceProfile.from_ratings(train, datetime.now().astimezone())
    summary = evaluate(test, profile, args.seed, args.pool_size)
    cv = cross_validate(rows, k=args.folds, seed=args.seed)
    quality = data_quality(rows)
    history = history_check(conn, profile)

    report = render_report(summary, quality, history, stats, cv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")

    print(report)
    print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
