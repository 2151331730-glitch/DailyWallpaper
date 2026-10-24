# tests/test_sugar.py
import pytest

from dailywallpaper.recommend.sugar import (
    NEUTRAL,
    SugarModel,
    build_vocabulary,
    fit,
    roc_auc,
)


def _row(style, mood, sugar):
    return {"style": list(style), "mood": list(mood), "sugar": sugar}


def test_roc_auc_is_one_for_perfect_separation():
    assert roc_auc([10, 9, 1, 0], [1, 1, 0, 0]) == 1.0


def test_roc_auc_is_zero_for_perfect_reversal():
    assert roc_auc([0, 1, 9, 10], [1, 1, 0, 0]) == 0.0


def test_roc_auc_counts_ties_as_half():
    assert roc_auc([5, 5], [1, 0]) == pytest.approx(0.5)


def test_roc_auc_needs_both_classes():
    assert roc_auc([1, 2], [1, 1]) != roc_auc([1, 2], [1, 1])  # NaN, not a lie


def test_vocabulary_drops_rare_tags():
    rows = [
        _row(["common", "rare"], [], "yes"),
        _row(["common"], [], "no"),
        _row(["common"], [], "yes"),
        _row(["common"], [], "no"),
        _row(["common"], [], "yes"),
        _row(["common"], [], "no"),
    ]
    vocabulary = build_vocabulary(rows, min_support=3)

    assert "common" in vocabulary
    assert "rare" not in vocabulary


def test_a_separable_batch_is_learned():
    rows = []
    for index in range(30):
        rows.append(_row(["neon"], ["vibrant"], "yes"))
        rows.append(_row(["abstract"], ["monochrome"], "no"))
    rows.append(_row(["neon"], ["vibrant"], "yes"))
    rows.append(_row(["abstract"], ["monochrome"], "no"))

    model = fit(rows)
    sugar = [_row(["neon"], ["vibrant"], "yes"),
             _row(["neon"], ["vibrant"], "yes")]
    plain = [_row(["abstract"], ["monochrome"], "no"),
             _row(["abstract"], ["monochrome"], "no")]

    assert model.score(sugar[0]["style"] + sugar[0]["mood"]) > 70
    assert model.score(plain[0]["style"] + plain[0]["mood"]) < 30


def test_unknown_tags_and_no_tags_are_neutral():
    rows = [_row(["neon"], [], "yes"), _row(["abstract"], [], "no")] * 8
    model = fit(rows)

    assert model.score([]) == NEUTRAL
    assert model.score(["never-seen-before"]) == NEUTRAL


def test_score_is_bounded():
    rows = [_row(["neon"], [], "yes"), _row(["abstract"], [], "no")] * 12
    model = fit(rows)

    for tags in (["neon"] * 20, ["abstract"] * 20):
        assert 0.0 <= model.score(tags) <= 100.0


def test_the_model_round_trips_through_json(tmp_path):
    rows = [_row(["neon"], [], "yes"), _row(["abstract"], [], "no")] * 8
    model = fit(rows)
    path = tmp_path / "sugar.json"
    model.save(path)

    loaded = SugarModel.load(path)
    assert loaded.score(["neon"]) == pytest.approx(model.score(["neon"]))
    assert loaded.score(["abstract"]) == pytest.approx(model.score(["abstract"]))


def test_regularisation_keeps_weights_small():
    # A single tag that perfectly separates could otherwise run to a huge
    # weight; the L2 term is what keeps the score readable.
    rows = [_row(["neon"], [], "yes")] * 20 + [_row(["abstract"], [], "no")] * 20
    model = fit(rows)
    assert max(abs(weight) for weight in model.weights) < 12
