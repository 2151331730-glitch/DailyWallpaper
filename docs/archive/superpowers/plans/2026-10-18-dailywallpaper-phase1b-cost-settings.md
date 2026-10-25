# DailyWallpaper Phase 1B-3（成本页、设置页、开机自启）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户看到本月花了多少钱、缓存命中多少，能在界面上改主要设置并写回 `config.toml`，并能一键开关开机自启。

**Architecture:** 两页都消费 Qt-free 的服务：`CostService` 读 `api_usage` 聚合，`SettingsService` 负责 `config.toml` 的读、改、写。开机自启走 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，由 `AutostartService` 封装。

**Tech Stack:** Python 3.13、PySide6 6.12、SQLite、`tomllib`（读）+ 手写最小 TOML 写出。

**Spec:** `docs/superpowers/specs/2026-10-17-dailywallpaper-design.md`（§10、§11.2）

## Global Constraints

- 不新增依赖：TOML 只读用标准库 `tomllib`，写出自己实现。
- 写 `config.toml` 会丢失注释；文件头必须写一行说明"由应用生成"。
- 设置写回后再读一次必须与内存中的 `Config` 相等（往返一致）。
- 开机自启只写 `HKCU`，不需要管理员权限；关闭时删除该值。
- 界面不阻塞：成本页一次查询完成，不做每 2 秒重算。
- 所有新代码 TDD；Qt 测试用 offscreen。

## Review Focus

1. `save_config` 往返：含 `pricing` 子表的配置写回后重新加载必须完全相等。
2. 成本页在没有任何调用记录时显示 0，不除零。
3. 缓存命中率：只有 `cache_hit=1` 与 `cache_hit=0` 两种行，命中率 = 命中 /（命中+未命中）。
4. 开机自启开启两次：注册表只有一个值，不重复。
5. 设置里写入非法值（如 `daily_time="25:99"`）：拒绝并保留原值，不写坏文件。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `src/dailywallpaper/cost/report.py` | `CostService`：月度汇总、按模型分组、缓存命中率、最近调用 |
| `src/dailywallpaper/config.py` | 增加 `save_config(path, cfg)` |
| `src/dailywallpaper/ui/cost_page.py` | 成本页 |
| `src/dailywallpaper/ui/settings_page.py` | 设置页 |
| `src/dailywallpaper/app_autostart.py` | `AutostartService`（注册表 Run 键） |
| `src/dailywallpaper/ui/main_window.py` | 增加两个标签页 |
| `tests/test_cost_report.py`、`tests/test_settings.py`、`tests/test_autostart.py`、`tests/test_ui_smoke.py` | 测试 |

---

### Task 1: CostService

**Files:** Create `src/dailywallpaper/cost/report.py`; Test `tests/test_cost_report.py`

**Interfaces:**
- `CostService(conn, clock)` → `.summary() -> CostSummary`
- `CostSummary(month, spent, budget, calls, cache_hits, by_model: list[ModelRow], recent: list[CallRow])`
- `ModelRow(model, calls, cost)`, `CallRow(ts, model, purpose, cost, cache_hit)`

- [ ] **Step 1: failing tests**

```python
# tests/test_cost_report.py
from datetime import datetime

from dailywallpaper.cost.report import CostService
from dailywallpaper.db import connect, migrate


class _Clock:
    def now(self):
        return datetime(2026, 10, 18, 12, 0)


def _usage(conn, ts, cost, model="glm-4.6v-flash", cache=0, status=200):
    conn.execute(
        "insert into api_usage(ts,provider,model,purpose,tokens_in,tokens_out,cost_cny,"
        "estimated,cache_hit,status_code,latency_ms) values(?,?,?,?,0,0,?,0,?,?,0)",
        (ts, "zhipu", model, "screen", cost, cache, status),
    )
    conn.commit()


def _service(tmp_path):
    conn = connect(tmp_path / "app.db"); migrate(conn)
    return conn, CostService(conn, _Clock())


def test_empty_ledger_is_all_zero(tmp_path):
    conn, service = _service(tmp_path)
    summary = service.summary()
    assert summary.spent == 0.0
    assert summary.calls == 0
    assert summary.cache_hit_rate(10.0) == 0.0


def test_only_the_current_month_counts(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5)
    _usage(conn, "2026-09-30T09:00:00", 9.0)
    assert service.summary().spent == 0.5


def test_4xx_rows_are_excluded_from_spend(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5)
    _usage(conn, "2026-10-02T10:00:00", 3.0, status=429)
    assert service.summary().spent == 0.5


def test_groups_by_model(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5, model="a")
    _usage(conn, "2026-10-02T09:10:00", 0.2, model="a")
    _usage(conn, "2026-10-02T09:20:00", 0.3, model="b")
    rows = {row.model: row for row in service.summary().by_model}
    assert rows["a"].calls == 2
    assert rows["a"].cost == 0.7
    assert rows["b"].calls == 1


def test_cache_hit_rate_counts_both_kinds(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5, cache=0)
    _usage(conn, "2026-10-02T09:10:00", 0.0, cache=1)
    _usage(conn, "2026-10-02T09:20:00", 0.0, cache=1)
    assert service.summary().cache_hit_rate(10.0) > 0.6


def test_recent_calls_are_newest_first(tmp_path):
    conn, service = _service(tmp_path)
    _usage(conn, "2026-10-02T09:00:00", 0.5)
    _usage(conn, "2026-10-03T09:00:00", 0.6)
    assert service.summary().recent[0].cost == 0.6
```

- [ ] **Step 2: run → FAIL (module missing)**
- [ ] **Step 3: implement**

```python
# src/dailywallpaper/cost/report.py
"""Read-only spend report for the cost tab (spec 10, 11.2)."""
from __future__ import annotations

from dataclasses import dataclass

MONTH_FILTER = "substr(ts,1,7)=? AND NOT (status_code BETWEEN 400 AND 499)"


@dataclass(frozen=True)
class ModelRow:
    model: str
    calls: int
    cost: float


@dataclass(frozen=True)
class CallRow:
    ts: str
    model: str
    purpose: str
    cost: float
    cache_hit: int


@dataclass(frozen=True)
class CostSummary:
    month: str
    spent: float
    calls: int
    cache_hits: int
    by_model: list
    recent: list

    def cache_hit_rate(self, _budget: float = 0.0) -> float:
        total = self.calls
        return 0.0 if total == 0 else self.cache_hits / total


class CostService:
    def __init__(self, conn, clock):
        self.conn = conn
        self.clock = clock

    def summary(self) -> CostSummary:
        month = self.clock.now().strftime("%Y-%m")
        spent = self.conn.execute(
            f"select coalesce(sum(cost_cny),0) from api_usage where {MONTH_FILTER}", (month,)
        ).fetchone()[0]
        calls = self.conn.execute(
            f"select count(*) from api_usage where {MONTH_FILTER}", (month,)
        ).fetchone()[0]
        hits = self.conn.execute(
            f"select count(*) from api_usage where {MONTH_FILTER} and cache_hit=1", (month,)
        ).fetchone()[0]
        by_model = [
            ModelRow(row[0], int(row[1]), float(row[2]))
            for row in self.conn.execute(
                f"""select model, count(*), coalesce(sum(cost_cny),0)
                    from api_usage where {MONTH_FILTER} group by model order by 3 desc""",
                (month,),
            ).fetchall()
        ]
        recent = [
            CallRow(row[0], row[1], row[2], float(row[3]), int(row[4]))
            for row in self.conn.execute(
                """select ts, model, purpose, cost_cny, cache_hit from api_usage
                   order by id desc limit 20"""
            ).fetchall()
        ]
        return CostSummary(month, float(spent), int(calls), int(hits), by_model, recent)
```

- [ ] **Step 4: run tests + full suite**
- [ ] **Step 5: commit** `feat: add the monthly cost report`

---

### Task 2: 成本页 + 设置页容器

**Files:** Create `src/dailywallpaper/ui/cost_page.py`; Modify `main_window.py`; Test `test_ui_smoke.py`

**Interfaces:** `CostPage(cost_service, budget_cny, parent=None)` with `.refresh()`; `MainWindow.tabs` gains "成本".

- [ ] **Step 1: failing test**

```python
def test_cost_page_shows_spend_and_models(qapp, tmp_path):
    from datetime import datetime
    from dailywallpaper.cost.report import CostService
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.cost_page import CostPage

    conn = connect(tmp_path / "app.db"); migrate(conn)
    conn.execute(
        "insert into api_usage(ts,provider,model,purpose,cost_cny,status_code)"
        " values('2026-10-02T09:00:00','zhipu','glm-x','screen',0.5,200)")
    conn.commit()

    class _Clock:
        def now(self):
            return datetime(2026, 10, 18, 12, 0)

    page = CostPage(CostService(conn, _Clock()), 10.0)
    page.refresh()

    assert "0.50" in page.spent_label.text()
    assert page.table.rowCount() == 1
```

- [ ] **Step 2: RED**, **Step 3: implement** — `QProgressBar` (spent/budget), `QLabel` for calls + hit rate, `QTableWidget` with columns 模型/调用/花费, plus a "最近调用" table.
- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: add the cost tab`

---

### Task 3: `save_config` 往返

**Files:** Modify `src/dailywallpaper/config.py`; Test `tests/test_settings.py`

**Interfaces:** `save_config(path: Path, cfg: Config) -> None`

- [ ] **Step 1: failing test**

```python
# tests/test_settings.py
from dailywallpaper.config import load_config, save_config


def test_round_trip_is_lossless(tmp_path):
    source = tmp_path / "in.toml"
    source.write_text(
        '[app]\ndata_dir = "data"\ndaily_time = "07:45"\nstart_with_windows = true\n'
        '[budget]\nmonthly_cny = 12.5\n'
        '[vision]\nscreen_model = "glm-x"\n'
        '[vision.pricing."glm-x"]\ninput_cny_per_million = 1.5\noutput_cny_per_million = 2.5\n',
        encoding="utf-8",
    )
    original = load_config(source)

    save_config(tmp_path / "out.toml", original)
    reloaded = load_config(tmp_path / "out.toml")

    assert reloaded == original


def test_saved_file_is_marked_as_generated(tmp_path):
    from dailywallpaper.config import Config
    from dailywallpaper.config import AppSection, SourcesSection, VisionSection, \
        BudgetSection, WeatherSection, WallpaperSection, WallpaperEngineSection, \
        ToolsSection, RetentionSection
    cfg = Config(AppSection(), SourcesSection(), VisionSection(), BudgetSection(),
                 WeatherSection(), WallpaperSection(), WallpaperEngineSection(),
                 ToolsSection(), RetentionSection())
    save_config(tmp_path / "c.toml", cfg)
    assert "by the app" in (tmp_path / "c.toml").read_text(encoding="utf-8").splitlines()[0]
```

- [ ] **Step 2: RED**, **Step 3: implement**

```python
def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    raise TypeError(f"unsupported config value: {value!r}")


def save_config(path: Path, cfg: Config) -> None:
    """Rewrite config.toml from ``cfg``. Comments are not preserved."""
    lines = ["# Generated by the app; edit through the settings page.", ""]
    sections = (
        ("app", cfg.app), ("sources", cfg.sources), ("budget", cfg.budget),
        ("weather", cfg.weather), ("wallpaper", cfg.wallpaper),
        ("wallpaper_engine", cfg.wallpaper_engine), ("tools", cfg.tools),
        ("retention", cfg.retention),
    )
    for name, section in sections:
        lines.append(f"[{name}]")
        for f in fields(section):
            lines.append(f"{f.name} = {_toml_value(getattr(section, f.name))}")
        lines.append("")
    vision = cfg.vision
    lines.append("[vision]")
    for f in fields(vision):
        if f.name == "pricing":
            continue
        lines.append(f"{f.name} = {_toml_value(getattr(vision, f.name))}")
    for model, price in vision.pricing.items():
        lines.append("")
        lines.append(f'[vision.pricing."{model}"]')
        lines.append(f"input_cny_per_million = {_toml_value(price.input_cny_per_million)}")
        lines.append(f"output_cny_per_million = {_toml_value(price.output_cny_per_million)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
```

（`config.py` 顶部加 `import json`。）

- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: write configuration back to toml`

---

### Task 4: 设置页

**Files:** Create `src/dailywallpaper/ui/settings_page.py`; Modify `main_window.py`; Test `test_ui_smoke.py`

**Interfaces:** `SettingsPage(config, config_path, on_saved=None)` with `.collect() -> Config` and `.save() -> bool`

- [ ] **Step 1: failing test** — 构造 `SettingsPage(cfg, path)`，改 `daily_time_edit` 为 `"07:15"`，点保存，重新 `load_config` 断言 `daily_time == "07:15"`；再把输入改成 `"25:99"` 点保存，断言返回 False 且文件未变。
- [ ] **Step 2: RED**, **Step 3: implement** — `QLineEdit`（daily_time / latest_change_time / city / 预算）、`QCheckBox`（wallhaven/bing 开关、开机自启、手动计入）、`QSpinBox`（分辨率下限），`collect()` 用 `dataclasses.replace` 生成新 `Config`，`save()` 先校验时间格式再 `save_config`。
- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: add the settings tab`

---

### Task 5: 开机自启

**Files:** Create `src/dailywallpaper/app_autostart.py`; Test `tests/test_autostart.py`

**Interfaces:** `AutostartService(registry=winreg, command=str)` with `.enabled()`, `.enable()`, `.disable()`

- [ ] **Step 1: failing test** — 用内存假注册表（`get/set/delete` 三个方法）断言：开启两次只有一个值；`disable` 后 `enabled()` 为 False；值名固定 `DailyWallpaper`。
- [ ] **Step 2: RED**, **Step 3: implement** — 走 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`，命令为 `"<pythonw.exe>" -m dailywallpaper --headless`。
- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: add the autostart toggle`

---

## 后续

- **1B-4**：`glm-4.6v` 付费复核前 5 名。
- 1B-1/1B-2 的 deferred Minor 见各自账本。
