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
- NDCG@5 needs several queries, so a test split is dealt into pools of about
  one day's candidate count. Forty-item pools look like the decision the app
  really faces. Every cross-validation fold deals its own pools, otherwise
  the whole report would rest on one or two queries and its standard
  deviation would be a constant zero.
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
    sugar_penalty,
)
from dailywallpaper.analysis.vision import VisionScores  # noqa: E402
from dailywallpaper.recommend.profile import PreferenceProfile  # noqa: E402
from dailywallpaper.recommend.sugar import SugarModel  # noqa: E402


def _sugar_model(path: Path | None = None):
    """The trained sugar model, or None (then the old nudge stays in charge)."""
    target = path or (ROOT / "data" / "sugar_model.json")
    if not Path(target).exists():
        return None
    try:
        return SugarModel.load(target)
    except Exception:
        return None

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
    numerator = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
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


def cross_validate(rows, k: int = 5, seed: int = SEED, min_gap: int = 1,
                   sugar=None, pool_size: int = POOL_SIZE,
                   top_k: int = TOP_K, min_pool: int | None = None) -> dict:
    """Pairwise accuracy and pool-level NDCG@5, both averaged over the folds.

    The profile is rebuilt from the training folds every time, so a picture
    that taught the profile never appears in the folds that judge it.

    NDCG@5 needs pools, and a single 80/20 split only yields one or two of
    them -- a mean with no spread, which cannot tell a real gap from luck.
    Dealing each fold's test split into its own pools gives every fold a
    couple of independent queries, so the reported standard deviation is the
    pool-to-pool swing the metric is meant to survive.
    """
    per_fold: dict[str, list[float]] = {}
    per_pool: dict[str, list[float]] = {}
    pairs_total = 0
    for train, test in kfold_split(rows, k, seed):
        profile = PreferenceProfile.from_ratings(train, datetime.now().astimezone())
        scored = [
            (score_variants(_scores_from_row(row), row["source"], profile,
                            sugar=sugar), row["rating"])
            for row in test
        ]
        if not scored:
            continue
        names = tuple(scored[0][0])
        for name in names:
            per_fold.setdefault(name, [])
            accuracy, pairs = pairwise_accuracy(
                [scores[name] for scores, _ in scored],
                [rating for _, rating in scored], min_gap)
            per_fold[name].append(accuracy)
            if name == "weighted":
                pairs_total += pairs

        floor = max(2, int(pool_size * 0.75)) if min_pool is None else min_pool
        for pool in make_pools(scored, pool_size, seed, min_size=floor):
            ideal = sorted((rating for _, rating in pool), reverse=True)
            for name in names:
                order = sorted(pool, key=lambda item: -item[0][name])
                per_pool.setdefault(name, []).append(
                    ndcg_at_k([rating for _, rating in order], ideal, top_k))
            rng = random.Random(seed)
            trials = []
            for _ in range(RANDOM_TRIALS):
                order = list(pool)
                rng.shuffle(order)
                trials.append(
                    ndcg_at_k([rating for _, rating in order], ideal, top_k))
            per_pool.setdefault("random", []).append(statistics.fmean(trials))

    summary = {}
    for name in per_fold:
        values = per_fold[name]
        pools = per_pool.get(name, [])
        summary[name] = {
            "accuracy_mean": statistics.fmean(values),
            "accuracy_sd": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "ndcg_mean": statistics.fmean(pools) if pools else 0.0,
            "ndcg_sd": statistics.pstdev(pools) if len(pools) > 1 else 0.0,
        }
    if per_pool.get("random"):
        values = per_pool["random"]
        summary["random"] = {
            "ndcg_mean": statistics.fmean(values),
            "ndcg_sd": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "pools": len(values),
        }
    summary["pairs"] = pairs_total
    summary["folds"] = k
    summary["pools"] = len(per_pool.get("weighted", []))
    summary["pool_size"] = pool_size
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
                   weather_code: int = 0, sugar=None) -> dict[str, float]:
    """The strategies worth comparing, on exactly the same picture.

    ``personalize`` carries the same cold-start smoothing the weighted score
    uses; without it a profile of a few ratings would look artificially good.

    ``sugar`` is the learned model; when it is present a fourth variant shows
    what ranking would do with the sugar penalty instead of the formulaic one.
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
    variants = {
        "explore": explore,
        "personalize": personalize,
        "blend": blend,
        "weighted": full.final,
    }
    if sugar is not None:
        tags = list(scores.style_tags) + list(scores.mood_tags)
        variants["sugar"] = blend - sugar_penalty(sugar.score(tags))
    return variants


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
             top_k: int = TOP_K, sugar=None) -> dict:
    """NDCG@5 per pseudo-pool, averaged; plus a whole-split Spearman."""
    scored = []
    for row in test_rows:
        scores = _scores_from_row(row)
        variants = score_variants(scores, row["source"], profile, sugar=sugar)
        scored.append((dict(row), variants))

    pools = make_pools(scored, size=pool_size, seed=seed)
    names = tuple(scored[0][1]) if scored else ()
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


def mix_label() -> str:
    """Name the weighting after the constants, not after a hard-coded ratio.

    The report kept saying 55/45 long after the weights were re-tuned; a
    label built from the constants cannot drift from the code that scores.
    """
    return f"{round(EXPLORE_WEIGHT * 100)}/{round(PERSONALIZE_WEIGHT * 100)}"


def render_report(summary, quality, history, stats, cv) -> str:
    mix = mix_label()
    strategy_labels = {
        "explore": "纯探索",
        "personalize": "纯个性化",
        "blend": f"{mix} 纯混合",
        "weighted": f"{mix} + 惩罚（原状）",
        "sugar": f"{mix} + 糖水判别式",
    }
    available = [name for name in strategy_labels if name in cv]
    best_label = strategy_labels[
        max(available, key=lambda name: cv[name]["accuracy_mean"])
    ]
    lines = [
        "# 离线评估报告",
        "",
        f"生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}",
        "",
        "由 `scripts/evaluate.py` 自动生成，数据全部来自 `data/app.db` 的缓存分析，",
        "不联网、不调用模型、不改动桌面。",
        "",
        "## 上一版报告为什么反过来了",
        "",
        "上一版报告的结论是「个性化那 45% 没有被证明有用」（纯个性化 0.572，"
        "低于纯探索 0.639）。那个结论测的是**一个缺陷**，不是个性化这条路：",
        "",
        "1. 评分权重固定以 5.5 为中点（`(分数 − 5.5) / 4.5`）。这个库 366 条评分"
        "平均 4.55，65% 落在 5.5 以下，于是画像学到的是「大部分都不喜欢」——"
        "标签权重求和为负，`tag_affinity` 对几乎所有候选都返回中性以下，"
        "个性化那一半再也不能说「更像这张」。",
        "2. 来源权重用的是**求和**，随图库规模增长。真实数据里 wallhaven 原始权重 −109，"
        "被 clamp 到端点，于是占候选 97% 的那个来源，每张图的来源分恒为 0.0。",
        "",
        "两处都改为以用户自己的均值为中点、来源取收缩后的均值。改完纯个性化从 0.572 升到 "
        f"{cv['personalize']['accuracy_mean']:.3f}，本报告下面的表格就是修复后的结果。"
        "这条记录留在报告里，是因为它比任何单个指标都说明问题：一个被 clamp 压平的项，"
        "在读指标的人眼里长得像「这条路没用」。",
        "",
        "## 数据",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 评分总数 | {stats['ratings']} |",
        f"| 有 {PROMPT_VERSION} 分析、可参与评估 | {stats['usable']} |",
        f"| 训练集（建画像） | {stats['train']} |",
        f"| 测试集（只评估） | {stats['test']} |",
        f"| 单次切分的伪候选池（仅用于 Spearman） "
        f"| {stats['pools']} 个 × {stats['pool_size']} 张 |",
        *([f"| 有评分但无法评估 | {stats['unevaluable']} 张"
           f"（模型返回值不合约，分析不落库） |"]
          if stats.get("unevaluable") else []),
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
            for name in ("explore", "personalize", "blend", "weighted", "sugar")
            if name in cv
        ],
        "",
        f"最好的策略：**{best_label}**",
        "",
        f"## NDCG@{TOP_K}（{cv['folds']} 折 × 每折 1–2 个候选池）",
        "",
        f"NDCG@{TOP_K} 按伪候选池（每池约 {cv['pool_size']} 张，模拟一天的候选量）"
        f"计算后取平均，共 **{cv['pools']} 个池**；括号内是池与池之间的标准差。"
        "池子由折内的测试集重新发牌得到，所以每折都有自己的排名题，"
        "离散程度是真实的、不是恒为 0 的占位值。",
        "",
        "| 排序策略 | NDCG@5 | 池间标准差 | 单次切分 Spearman |",
        "|---|---|---|---|",
    ]
    labels = {
        "random": "随机（下限）",
        "explore": "纯探索",
        "personalize": "纯个性化",
        "blend": f"{mix} 纯混合",
        "weighted": f"{mix} + 惩罚（不含糖水模型）",
        "sugar": f"{mix} + 糖水判别式",
    }
    order = ("random", "explore", "personalize", "blend", "weighted", "sugar")
    for name in order:
        if name not in cv:
            continue
        row = cv[name]
        spearman = summary[name]["spearman"] if name in summary else None
        lines.append(
            f"| {labels[name]} | {_fmt(row['ndcg_mean'])} "
            f"(±{_fmt(row['ndcg_sd'])}) | "
            f"{'—' if spearman is None else _fmt(spearman)} |"
        )
    scored_names = [name for name in ("explore", "personalize", "blend",
                                      "weighted", "sugar") if name in cv]
    best = max(scored_names, key=lambda name: cv[name]["ndcg_mean"])
    lines += [
        "",
        f"最好的策略：**{labels[best]}**",
        "",
        "Spearman 来自单次 70/30 切分（整个测试集一条排名），只作方向参考；"
        "两种指标不一致时以成对比较为准。",
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
    if stats.get("by_source"):
        share = "、".join(f"{name} {count * 100 // stats['usable']}%"
                         for name, count in stats["by_source"].items())
        composition = f"评估集的来源构成：{share}。"
    else:
        composition = ""
    lines += [
        "## 局限",
        "",
        "- " + (composition + "静态图占绝大多数，结论主要说明静态图的审美排序，"
                "不代表 Steam 动态壁纸。" if composition else
                "结论主要说明静态图的审美排序，不代表 Steam 动态壁纸。"),
        "- 离线回放测的是排序逻辑；它回答不了“换个来源能不能找到更好的图”。",
        *([f"- 有 {stats['unevaluable']} 张评分没有分析：视觉模型连续返回不合约的字段"
           "（重新编码后再试仍失败），这几张只能排除在指标之外。"]
          if stats.get("unevaluable") else []),
        "- 四维分数完全相同的组数很多，说明模型对相近的图给出的分辩不出差别；"
        "排序里那部分名次由探索分和画像分决定，不是由画质分决定。",
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
    by_source: dict[str, int] = {}
    for row in rows:
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1
    stats = {"ratings": count_ratings(conn), "usable": len(rows),
             "unevaluable": count_ratings(conn) - len(rows),
             "by_source": dict(sorted(by_source.items(),
                                      key=lambda item: -item[1]))}
    train, test = split_rows(rows, args.test_ratio, args.seed)
    stats.update(train=len(train), test=len(test),
                 pool_size=args.pool_size,
                 pools=len(make_pools(test, args.pool_size, args.seed)))

    profile = PreferenceProfile.from_ratings(train, datetime.now().astimezone())
    sugar = _sugar_model()
    summary = evaluate(test, profile, args.seed, args.pool_size, sugar=sugar)
    cv = cross_validate(rows, k=args.folds, seed=args.seed, sugar=sugar,
                        pool_size=args.pool_size, top_k=TOP_K)
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
