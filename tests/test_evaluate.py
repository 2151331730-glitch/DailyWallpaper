# tests/test_evaluate.py
import importlib.util
from pathlib import Path

import pytest


def _module():
    spec = importlib.util.spec_from_file_location(
        "evaluate", Path("scripts/evaluate.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ndcg_is_one_for_the_ideal_order():
    ev = _module()
    ideal = [10, 8, 5, 2, 1]
    assert ev.ndcg_at_k(ideal, ideal, 5) == 1.0


def test_ndcg_drops_when_the_best_sits_last():
    ev = _module()
    ideal = [10, 8, 5, 2, 1]
    worst = list(reversed(ideal))
    assert ev.ndcg_at_k(worst, ideal, 5) < ev.ndcg_at_k(ideal, ideal, 5)
    # Reversed still keeps the 5 in the middle and only loses the head, so it
    # lands near 0.64 far from 1.0; the point is the penalty, not a fixed score.
    assert 0.5 < ev.ndcg_at_k(worst, ideal, 5) < 0.75


def test_ndcg_at_k_ignores_everything_past_the_head():
    ev = _module()
    ideal = [10, 8, 5, 2, 1]
    # Same head, different tail: @3 must not change.
    assert ev.ndcg_at_k([10, 8, 5, 1, 2], ideal, 3) == ev.ndcg_at_k(
        [10, 8, 5, 2, 1], ideal, 3
    )


def test_ndcg_is_zero_when_the_ideal_gain_is_zero():
    ev = _module()
    assert ev.ndcg_at_k([0, 0], [0, 0], 2) == 0.0


def test_spearman_is_one_for_identical_and_minus_one_for_reversed():
    ev = _module()
    xs = [1, 2, 3, 4]
    assert ev.spearman(xs, xs) == pytest.approx(1.0)
    assert ev.spearman(xs, list(reversed(xs))) == pytest.approx(-1.0)


def test_spearman_handles_ties_without_dividing_by_zero():
    ev = _module()
    flat = [5, 5, 5, 5]
    assert ev.spearman(flat, [1, 2, 3, 4]) == 0.0


def test_split_keeps_the_rating_mix():
    ev = _module()
    rows = [
        {"rating": value, "id": index * 3 + offset}
        for index in range(10)
        for offset, value in enumerate((1, 5, 10))
    ]
    train, test = ev.split_rows(rows, test_ratio=0.3, seed=7)

    assert len(train) + len(test) == len(rows)
    assert {row["id"] for row in train} & {row["id"] for row in test} == set()
    for group in (train, test):
        assert {row["rating"] for row in group} == {1, 5, 10}


def test_split_is_deterministic_for_a_seed():
    ev = _module()
    rows = [{"rating": index % 10 + 1, "id": index} for index in range(50)]
    first = ev.split_rows(rows, test_ratio=0.3, seed=7)
    second = ev.split_rows(rows, test_ratio=0.3, seed=7)
    assert [row["id"] for row in first[1]] == [row["id"] for row in second[1]]


def test_pools_use_every_item_at_most_once():
    ev = _module()
    items = list(range(25))
    pools = ev.make_pools(items, size=10, seed=3)

    seen = [item for pool in pools for item in pool]
    assert len(seen) == len(set(seen))
    assert set(seen) <= set(items)


def test_pools_drop_a_short_remainder_rather_than_score_noise():
    ev = _module()
    pools = ev.make_pools(list(range(23)), size=10, seed=3, min_size=10)
    assert all(len(pool) >= 10 for pool in pools)
    assert sum(len(pool) for pool in pools) <= 23


def test_relevance_matches_the_human_rating_of_the_ranked_items():
    ev = _module()
    ranked = [{"rating": 2}, {"rating": 9}]
    assert ev.relevance(ranked) == [2, 9]


# ------------------------------------------------------- pairwise metric ---

def test_pairwise_accuracy_is_one_for_a_perfect_ranker():
    ev = _module()
    ratings = [1, 4, 7, 10]
    scores = [10.0, 20.0, 30.0, 40.0]
    accuracy, pairs = ev.pairwise_accuracy(scores, ratings)
    assert accuracy == 1.0
    assert pairs == 6


def test_pairwise_accuracy_is_zero_for_a_reversed_ranker():
    ev = _module()
    ratings = [1, 4, 7, 10]
    scores = [40.0, 30.0, 20.0, 10.0]
    accuracy, _ = ev.pairwise_accuracy(scores, ratings)
    assert accuracy == 0.0


def test_pairwise_ties_count_as_half():
    ev = _module()
    ratings = [1, 10]
    scores = [5.0, 5.0]
    accuracy, pairs = ev.pairwise_accuracy(scores, ratings)
    assert pairs == 1
    assert accuracy == pytest.approx(0.5)


def test_pairwise_ignores_pairs_closer_than_the_gap():
    ev = _module()
    ratings = [5, 6, 10]
    scores = [1.0, 2.0, 3.0]
    # With a gap of 2 only the two (5,10) and (6,10) pairs count.
    _, pairs = ev.pairwise_accuracy(scores, ratings, min_gap=2)
    assert pairs == 2


def test_pairwise_returns_no_pairs_when_nothing_differs():
    ev = _module()
    accuracy, pairs = ev.pairwise_accuracy([1.0, 2.0], [5, 5])
    assert pairs == 0
    assert accuracy == 0.0


# ------------------------------------------------------- cross validation ---

def _rows(count=50):
    return [
        {"rating": index % 10 + 1, "id": index, "created_at": "2000-01-01T00:00:00"}
        for index in range(count)
    ]


def test_kfold_puts_every_row_in_exactly_one_test_fold():
    ev = _module()
    rows = _rows(50)
    folds = ev.kfold_split(rows, k=5, seed=7)

    assert len(folds) == 5
    tested = [row["id"] for _, test in folds for row in test]
    assert len(tested) == len(rows)
    assert set(tested) == {row["id"] for row in rows}
    for train, test in folds:
        assert {row["id"] for row in train} & {row["id"] for row in test} == set()


def test_kfold_keeps_the_rating_mix_in_every_fold():
    ev = _module()
    rows = _rows(50)
    for _, test in ev.kfold_split(rows, k=5, seed=7):
        assert {row["rating"] for row in test} == set(range(1, 11))


def test_kfold_is_deterministic_for_a_seed():
    ev = _module()
    first = ev.kfold_split(_rows(50), k=5, seed=7)
    second = ev.kfold_split(_rows(50), k=5, seed=7)
    assert [row["id"] for _, test in first for row in test] == [
        row["id"] for _, test in second for row in test
    ]
