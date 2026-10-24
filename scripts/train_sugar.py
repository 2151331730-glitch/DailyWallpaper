# scripts/train_sugar.py
"""Train the tag-based sugar-water model and say how well it generalises.

Five-fold cross validation, stratified by label. The number that matters is
the pooled out-of-fold AUC: each picture is scored by a model that never saw
it. A model fitted on all 99 labels and then judged on those same labels
would only measure how well it memorised them.

    .venv\\Scripts\\python scripts\\train_sugar.py
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--min-support", type=int, default=MIN_SUPPORT)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "sugar_model.json")
    args = parser.parse_args(argv)

    conn = connect(ROOT / "data" / "app.db")
    migrate(conn)
    rows = [row for row in _concept().load_batch(conn) if row["origin"] != "we"]
    labeled = [row for row in rows if row["sugar"] in ("yes", "no")]
    if len(labeled) < 30:
        print(f"明确标注只有 {len(labeled)} 条，至少需要 30 条")
        return 1

    folds = stratified_folds(labeled, args.folds, args.seed)
    pooled_scores, pooled_labels, per_fold = [], [], []
    for index, test in enumerate(folds):
        train = [row for other, fold in enumerate(folds) if other != index
                 for row in fold]
        model = fit(train, min_support=args.min_support)
        scores = [model.score(row["style"] + row["mood"]) for row in test]
        labels = [1 if row["sugar"] == "yes" else 0 for row in test]
        pooled_scores += scores
        pooled_labels += labels
        value = roc_auc(scores, labels)
        per_fold.append(value)
        print(f"  折 {index + 1}: n={len(test)} AUC={value:.3f}")

    pooled = roc_auc(pooled_scores, pooled_labels)
    print(f"\n合并 AUC（每张图都由没见过它的模型打分）: {pooled:.3f}")
    print(f"样本 {len(labeled)} 条（糖水 {sum(1 for r in labeled if r['sugar'] == 'yes')}"
          f" / 不是 {sum(1 for r in labeled if r['sugar'] == 'no')}），"
          f"标签词表 {len(build_vocabulary(labeled, args.min_support))} 个")

    model = fit(labeled, min_support=args.min_support)
    model.save(args.out)
    pairs = sorted(zip(model.tags, model.weights), key=lambda pair: pair[1])
    print("\n最像糖水的标签（权重越高越像）：")
    for tag, weight in pairs[-8:][::-1]:
        print(f"  {tag:20s} {weight:+.2f}")
    print("最不像糖水的标签：")
    for tag, weight in pairs[:8]:
        print(f"  {tag:20s} {weight:+.2f}")
    print(f"\n模型已保存到 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
