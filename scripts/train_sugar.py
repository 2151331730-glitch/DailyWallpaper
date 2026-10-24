# scripts/train_sugar.py
"""Train the tag-based sugar-water model and say how well it generalises.

Two numbers, because they answer different questions:

- **Holdout**: fit on the first batch (sampled by keywords), test on the
  second batch's *random* group (sampled without looking at the label). No
  picture in the test set was chosen for any reason, and none of them taught
  the model. This is the number to believe.
- **Merged k-fold**: five folds over every definite label from both batches.
  Useful for comparing models, but the second batch's "uncertain" group was
  selected *because* the model found it hard, so it runs low by design.

    .venv\\Scripts\\python scripts/train_sugar.py
"""
from __future__ import annotations

import argparse
import importlib.util
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dailywallpaper.db import connect, migrate  # noqa: E402
from dailywallpaper.recommend.sugar import (  # noqa: E402
    MIN_SUPPORT,
    build_vocabulary,
    fit,
    roc_auc,
)


def _concept():
    spec = importlib.util.spec_from_file_location(
        "concept_report", ROOT / "scripts" / "concept_report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stratified_folds(rows, k: int, seed: int):
    yes = [row for row in rows if row["sugar"] == "yes"]
    no = [row for row in rows if row["sugar"] == "no"]
    rng = random.Random(seed)
    rng.shuffle(yes)
    rng.shuffle(no)
    folds = [[] for _ in range(k)]
    for index, row in enumerate(yes):
        folds[index % k].append(row)
    for index, row in enumerate(no):
        folds[index % k].append(row)
    return folds


def _score(model, row) -> float:
    return model.score(row["style"] + row["mood"])


def _labels(rows):
    return [1 if row["sugar"] == "yes" else 0 for row in rows]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--min-support", type=int, default=MIN_SUPPORT)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "sugar_model.json")
    args = parser.parse_args(argv)

    concept = _concept()
    conn = connect(ROOT / "data" / "app.db")
    migrate(conn)
    batch1 = concept.load_batch(conn, ROOT / "data" / "concept_batch")
    batch2 = concept.load_batch(conn, ROOT / "data" / "concept_batch2")
    batch3 = concept.load_batch(conn, ROOT / "data" / "concept_batch3")

    def definite(row):
        return row["sugar"] in ("yes", "no")

    # Wallpaper Engine items were labelled from covers, so they stay out.
    first = [row for row in batch1 if row["origin"] != "we" and definite(row)]
    second = [row for row in batch2 if definite(row)]
    second_random = [row for row in batch2 if row["origin"] == "random" and definite(row)]
    live = [row for row in batch3 if definite(row)]
    merged = first + second

    print(f"第一批 {len(first)} 条")
    print(f"第二批 {len(second)} 条（其中随机组 {len(second_random)}）")
    print(f"第三批（线上分布）{len(live)} 条，"
          f"糖水 {sum(1 for r in live if r['sugar'] == 'yes')} / "
          f"不是 {sum(1 for r in live if r['sugar'] == 'no')}")
    print(f"训练用 {len(merged)} 条，标签词表 "
          f"{len(build_vocabulary(merged, args.min_support))} 个\n")

    if first and second_random:
        model = fit(first, min_support=args.min_support)
        value = roc_auc([_score(model, row) for row in second_random],
                        _labels(second_random))
        print(f"参考：第一批训练 → 第二批随机组  AUC {value:.3f}  n={len(second_random)}")

    if merged and live:
        model = fit(merged, min_support=args.min_support)
        value = roc_auc([_score(model, row) for row in live], _labels(live))
        print(f"**决定性：前两批训练 → 第三批（线上分布）**  AUC {value:.3f}  n={len(live)}")
    else:
        print("第三批为空，跳过留出验证")

    folds = stratified_folds(merged + live, args.folds, args.seed)
    pooled_scores, pooled_labels = [], []
    for index, test in enumerate(folds):
        train = [row for other, fold in enumerate(folds) if other != index
                 for row in fold]
        model = fit(train, min_support=args.min_support)
        scores = [_score(model, row) for row in test]
        pooled_scores += scores
        pooled_labels += _labels(test)
        print(f"  折 {index + 1}: n={len(test)} AUC={roc_auc(scores, _labels(test)):.3f}")
    pooled = roc_auc(pooled_scores, pooled_labels)
    print(f"合并 5 折 AUC: {pooled:.3f}（全样本；含按模型挑出的难例，偏低）")

    model = fit(merged + live, min_support=args.min_support)
    model.save(args.out)
    pairs = sorted(zip(model.tags, model.weights), key=lambda pair: pair[1])
    print("\n最像糖水的标签：")
    for tag, weight in pairs[-8:][::-1]:
        print(f"  {tag:20s} {weight:+.2f}")
    print("最不像糖水的标签：")
    for tag, weight in pairs[:8]:
        print(f"  {tag:20s} {weight:+.2f}")
    print(f"\n模型已保存到 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
