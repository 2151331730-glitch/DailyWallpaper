# src/dailywallpaper/recommend/sugar.py
"""Sugar-water discriminator learned from the labelled batch.

The vision model's own formulaic score separates sugar water at AUC 0.577 --
barely better than a coin (docs/SUGARWATER.md). Its tags do far better:
cyberpunk 88% sugar water against a 54% base rate, abstract 9%. So the
concept is learned from the tags with a small regularised logistic
regression, instead of a hand-tuned weight on a score that cannot see it.

The model is deliberately conservative: without a single known tag it says
nothing (NEUTRAL) rather than guessing, and L2 keeps any one tag from
dominating the score.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# A tag seen fewer times than this cannot teach anything reliable.
MIN_SUPPORT = 6
# Strong enough that a perfectly separating tag settles around weight 1.2
# instead of running off, so the 0..100 score stays readable.
L2 = 0.1
LEARNING_RATE = 0.5
ITERATIONS = 1500
NEUTRAL = 50.0

# Words the user named as sugar water (2026-10-25): luxury/sports cars and the
# 哪吒 character. The learned model cannot know them -- its vocabulary is
# English style/mood tags with no training support for these -- so they are
# matched against the title and the tags as a flat "this is sugar water" vote.
# Deliberately specific: a bare "car" would catch every picture with a vehicle
# in it, and the model already carries a small weight for that tag.
SUGAR_WORDS = (
    "豪车", "跑车", "超跑", "名车", "兰博基尼", "法拉利", "保时捷", "布加迪",
    "哪吒",
    "supercar", "sports-car", "sportscar", "luxury-car", "luxurycar",
    "lamborghini", "ferrari", "porsche", "bugatti",
)
# What a named word is worth on the model's 0..100 scale. Anything above the
# neutral 50 costs points (see analysis/scoring.sugar_penalty); this is high
# enough that one hit outweighs a model that says nothing.
SUGAR_WORD_SCORE = 90.0


def sugar_word_hit(*texts) -> bool:
    """True when any of these strings contains a word the user called out."""
    haystack = " ".join(str(text).lower() for text in texts if text)
    if not haystack:
        return False
    return any(word in haystack for word in SUGAR_WORDS)


def _tags(row) -> list[str]:
    out = []
    for key in ("style", "mood"):
        for tag in row.get(key) or ():
            if isinstance(tag, str):
                value = tag.strip().lower()
                if value:
                    out.append(value)
    return out


def build_vocabulary(rows, min_support: int = MIN_SUPPORT) -> list[str]:
    counts = Counter(tag for row in rows for tag in _tags(row))
    return sorted(tag for tag, count in counts.items() if count >= min_support)


def roc_auc(scores, labels) -> float:
    """Probability a sugar-water picture scores above a non-sugar one.

    0.5 is a coin. Ties count half. Rank-based (Mann-Whitney) so no
    statistics library is needed.
    """
    pairs = [(float(score), label) for score, label in zip(scores, labels, strict=False)
             if label in (0, 1)]
    positives = [score for score, label in pairs if label == 1]
    negatives = [score for score, label in pairs if label == 0]
    if not positives or not negatives:
        return float("nan")
    values = [score for score, _ in pairs]
    order = sorted(range(len(values)), key=lambda index: values[index])
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
    rank_sum = sum(ranks[index] for index, (_, label) in enumerate(pairs) if label == 1)
    return (rank_sum - len(positives) * (len(positives) + 1) / 2.0) / (
        len(positives) * len(negatives))


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


@dataclass
class SugarModel:
    tags: list[str]
    weights: list[float]
    bias: float = 0.0
    min_support: int = MIN_SUPPORT
    _index: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        self._index = {tag: index for index, tag in enumerate(self.tags)}

    def score(self, tags) -> float:
        """0 (nothing like sugar water) .. 100 (exactly the pattern), or NEUTRAL.

        A picture whose tags are all outside the vocabulary gets NEUTRAL: the
        model has no opinion, and an opinion invented from nothing is worse
        than silence.
        """
        total = 0.0
        matched = False
        for tag in tags or ():
            if not isinstance(tag, str):
                continue
            index = self._index.get(tag.strip().lower())
            if index is not None:
                total += self.weights[index]
                matched = True
        if not matched:
            return NEUTRAL
        return 100.0 * _sigmoid(self.bias + total)

    def save(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({
            "tags": self.tags,
            "weights": self.weights,
            "bias": self.bias,
            "min_support": self.min_support,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SugarModel":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(tags=payload["tags"], weights=payload["weights"],
                   bias=payload.get("bias", 0.0),
                   min_support=payload.get("min_support", MIN_SUPPORT))


def fit(rows, min_support: int = MIN_SUPPORT, l2: float = L2,
        learning_rate: float = LEARNING_RATE, iterations: int = ITERATIONS) -> SugarModel:
    """Train on rows shaped {"style": [...], "mood": [...], "sugar": yes|no}.

    "unsure" rows are dropped: a label the labeller could not decide would
    only push the weights toward the middle.
    """
    vocabulary = build_vocabulary(rows, min_support)
    index = {tag: position for position, tag in enumerate(vocabulary)}
    samples, labels = [], []
    for row in rows:
        label = row.get("sugar")
        if label not in ("yes", "no"):
            continue
        vector = [0.0] * len(vocabulary)
        for tag in set(_tags(row)):
            position = index.get(tag)
            if position is not None:
                vector[position] = 1.0
        samples.append(vector)
        labels.append(1.0 if label == "yes" else 0.0)

    weights = [0.0] * len(vocabulary)
    bias = 0.0
    if not samples or len(set(labels)) < 2:
        return SugarModel(vocabulary, weights, bias, min_support)

    count = len(samples)
    for _ in range(iterations):
        gradient = [0.0] * len(vocabulary)
        bias_gradient = 0.0
        for vector, target in zip(samples, labels, strict=False):
            total = bias + sum(
                weight * value for weight, value in zip(weights, vector, strict=False)
            )
            error = _sigmoid(total) - target
            bias_gradient += error
            for position, value in enumerate(vector):
                if value:
                    gradient[position] += error * value
        for position in range(len(weights)):
            # L2 on every step is what keeps a perfectly separating tag from
            # running off to an unreadable weight.
            weights[position] -= learning_rate * (
                gradient[position] / count + l2 * weights[position])
        bias -= learning_rate * (bias_gradient / count)
    return SugarModel(vocabulary, weights, bias, min_support)
