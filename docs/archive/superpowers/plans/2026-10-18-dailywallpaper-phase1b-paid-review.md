# DailyWallpaper Phase 1B-4（付费复核）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用付费的 `glm-4.6v` 复核免费模型筛出的前 5 名，用复核分数重新排序，并在界面上标出哪些是复核过的。

**Architecture:** 复核是 `RealPipeline.prepare()` 的最后一步：排序后取前 `review_top_n` 张，用 `VisionClient.analyze_or_cache(..., review_model)` 重新分析（会自动走缓存与预算），再用复核分数重算 `DecisionScores` 并重新排序。复核失败或预算不足时保留免费模型的分数，不阻断当天。

**Tech Stack:** 现有 `VisionClient`、`BudgetGuard`、`final_score`、`rank`。

**Spec:** `docs/superpowers/specs/2026-10-17-dailywallpaper-design.md`（§4.4、§8.6、§10）

## Global Constraints

- 复核**必须**先过 `budget.check()`；预算不足立即停止，保留已复核的结果。
- 复核结果复用 `analyses` 的 `(image_hash, model, prompt_version)` 缓存，同一张图不会重复付费。
- 复核失败（`VisionError`、429）只记日志，不影响当天出图。
- 复核后仍要通过 `hard_gate`；不通过的从候选池移除。
- 不新增依赖。

## Review Focus

1. 预算刚好够 3 次复核而 `review_top_n=5`：只复核 3 张，剩下的保留免费分数。
2. 复核结果被硬门槛拒绝：该候选从候选池移除，不会因为"分数更高"反而被选中。
3. 同一张图连续两天进入前 5：第二次走缓存，`api_usage` 记 `cache_hit=1`、成本 0。
4. 复核模型未在 `[vision.pricing]` 里配置：抛配置错误而不是 `KeyError`。
5. `review_top_n=0`：完全跳过复核。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `src/dailywallpaper/app.py` | `RealPipeline._review_top()` + 在 `prepare()` 末尾调用 |
| `src/dailywallpaper/models.py` | `Decision` 增加 `reviewed: bool = False` |
| `src/dailywallpaper/ui/main_window.py` | 今日页显示"已复核"标记 |
| `tests/test_integration.py` | 复核行为测试 |

---

### Task 1: 复核候选

**Files:** Modify `src/dailywallpaper/models.py`、`src/dailywallpaper/app.py`；Test `tests/test_integration.py`

**Interfaces:**
- `Decision(wallpaper_id, image_path, scores, explore, personalize, final, reason, reviewed=False)`
- `RealPipeline._review_top(profile, recent, weather_code) -> None`（原地改写 `self.shortlist`）

- [ ] **Step 1: failing tests**

```python
def test_review_replaces_the_screen_scores(tmp_path):
    # provider returns quality 91 for the review model and 70 for the screen model
    ...
    pipeline.prepare(clock.now())
    assert pipeline.shortlist[0].reviewed is True
    assert pipeline.shortlist[0].scores.quality == 91


def test_review_stops_when_the_budget_is_gone(tmp_path):
    # budget allows one review; three candidates qualify
    assert sum(1 for d in pipeline.shortlist if d.reviewed) == 1


def test_review_failure_keeps_the_screen_scores(tmp_path):
    # provider raises for the review model
    assert pipeline.shortlist[0].reviewed is False
    assert pipeline.shortlist  # still a usable candidate


def test_review_top_n_zero_skips_review(tmp_path):
    assert all(not d.reviewed for d in pipeline.shortlist)
```

（伪 provider 按 `model` 返回不同分数；`FakeBudget.check` 可配置返回值的次数。）

- [ ] **Step 2: run → FAIL**
- [ ] **Step 3: implement**

`models.py`:

```python
@dataclass(frozen=True)
class Decision:
    wallpaper_id: int
    image_path: Path
    scores: "VisionScores"
    explore: float
    personalize: float
    final: float
    reason: str
    reviewed: bool = False
```

`app.py`，在 `prepare()` 的 `self.shortlist = rank(...)` 之后：

```python
        if self.cfg.vision.review_top_n > 0 and self.shortlist:
            self._review_top(profile, recent, weather_code)
            self.shortlist = rank(self.shortlist, now.date().isoformat())
```

新增方法：

```python
    def _review_top(self, profile, recent, weather_code) -> None:
        """Re-score the strongest candidates with the paid model.

        A failure or an exhausted budget stops the loop and keeps whatever
        has been reviewed so far; the free screen scores stay valid.
        """
        kept = []
        reviewed_ids = set()
        top = self.shortlist[: self.cfg.vision.review_top_n]
        for decision in top:
            if not self.budget.check(ESTIMATE_CNY):
                logger.warning("review stopped: budget exhausted")
                break
            image_hash = Path(decision.image_path).stem
            try:
                record = self.vision.analyze_or_cache(
                    decision.wallpaper_id,
                    image_hash,
                    decision.image_path,
                    self.cfg.vision.review_model,
                )
            except Exception as exc:
                logger.warning(f"review failed for {decision.wallpaper_id}: {exc}")
                break
            if hard_gate(record.scores):
                continue
            scores = final_score(
                record.scores, decision.source, profile, recent, weather_code
            )
            kept.append(
                replace(
                    decision,
                    scores=record.scores,
                    explore=scores.explore,
                    personalize=scores.personalize,
                    final=scores.final,
                    reviewed=True,
                )
            )
            reviewed_ids.add(decision.wallpaper_id)
        rest = [
            decision
            for decision in self.shortlist
            if decision.wallpaper_id not in reviewed_ids
        ]
        self.shortlist = kept + rest
```

`Decision` 需要 `source` 才能重算——加一个 `source: str = ""` 字段并在构造处传 `candidate.source`。

- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: review the top candidates with the paid model`

---

### Task 2: 界面标记与配置校验

**Files:** Modify `src/dailywallpaper/ui/main_window.py`、`src/dailywallpaper/app.py`；Test `tests/test_ui_smoke.py`、`tests/test_integration.py`

- [ ] **Step 1: failing tests**
  - 今日页：`decisions` 里最新一行的壁纸若在 `analyses` 里有 `model == cfg.vision.review_model` 的行，`main_window.reviewed_label.text()` 含"已复核"。
  - `build_pipeline`：`cfg.vision.pricing` 缺少 `review_model` 时抛 `ValueError`（消息含模型名），而不是在第一次复核时 `KeyError`。
- [ ] **Step 2: RED**, **Step 3: implement** — 今日页加一个 `reviewed_label`；`build_pipeline` 启动时校验 `screen_model` 与 `review_model` 都在 pricing 里。
- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: show the review badge and validate pricing`

---

## 后续

- Phase 1C：动态壁纸（WE scene/video）、保留策略、打包。
