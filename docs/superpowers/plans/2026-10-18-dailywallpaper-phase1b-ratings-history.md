# DailyWallpaper Phase 1B-2（评分与历史）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户能给当前壁纸打分（喜欢/不喜欢/中性 + 快捷理由 + 自由文本），并在历史页回看最近 90 天，以及从历史页撤销最近一次更换。

**Architecture:** 评分写入与查询放进 Qt-free 的 `RatingsService`（可纯 pytest 测）；评分窗和历史页只负责收集输入与渲染。评分落 `ratings` 表（每张壁纸一行，`ux_ratings_wallpaper` 唯一），`derived_tags_json` 取该壁纸最新一次分析的主标签，供 `PreferenceProfile` 直接消费。

**Tech Stack:** Python 3.13、PySide6 6.12、SQLite、pytest（Qt 用 offscreen）。

**Spec:** `docs/superpowers/specs/2026-10-17-dailywallpaper-design.md`（§8.7、§11.2、§11.3）

## Global Constraints

- 不新增依赖。
- 后台/网络逻辑不进 UI 线程。
- 评分为插入或替换（同一壁纸只保留一行），不改 `change_log`。
- 文字评价本轮只存 `comment`，不做标签抽取（留给 Phase 2）。
- 失败不弹窗；原因写日志与状态区。
- 所有新代码 TDD；Qt 测试用 `QT_QPA_PLATFORM=offscreen`。

## Review Focus

1. 同一张壁纸连续评两次：`ratings` 只有一行，第二次覆盖第一次。
2. 对没有分析记录的壁纸评分：`derived_tags_json` 退化为 `[]`，不抛异常。
3. 快捷理由与三态互不干扰：选了"太暗"再选"不喜欢"，两者都保存。
4. 历史页对一条已被删除的本地文件：显示占位而不是崩。
5. 撤销一条历史记录：调用 `ApplyService.undo_last`，不伪造 `change_log`。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `src/dailywallpaper/recommend/ratings.py` | `RatingsService`：评分写入/查询/历史，**不 import Qt** |
| `src/dailywallpaper/ui/rating_dialog.py` | 评分窗 |
| `src/dailywallpaper/ui/history_page.py` | 历史页 |
| `src/dailywallpaper/ui/main_window.py` | 改为标签页容器（今日 / 历史） |
| `src/dailywallpaper/ui/tray.py` | 喜欢/不喜欢 菜单项接线 |
| `tests/test_ratings.py` / `tests/test_ui_smoke.py` | 测试 |

---

### Task 1: RatingsService（无 Qt）

**Files:**
- Create: `src/dailywallpaper/recommend/ratings.py`
- Test: `tests/test_ratings.py`

**Interfaces:**
- Consumes: `ratings`、`wallpaper_analyses`、`analyses`、`wallpapers` 表
- Produces:
  - `RatingsService(conn)`
  - `.rate(wallpaper_id: int, rating: int, comment: str = "", reasons: list[str] | None = None) -> None`
  - `.rating_for(wallpaper_id: int) -> int | None`
  - `.history(days: int = 90, limit: int = 90) -> list[HistoryRow]`
  - `HistoryRow(wallpaper_id, title, local_path, decision_date, final_score, rating, comment)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ratings.py
import pytest

from dailywallpaper.db import connect, migrate
from dailywallpaper.recommend.ratings import RatingsService


def _setup(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into wallpapers(source,source_id,title,local_path,created_at)"
        " values('bing','1','测试壁纸','C:/x.jpg','x')"
    )
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    return conn, wid


def test_first_rating_is_inserted(tmp_path):
    conn, wid = _setup(tmp_path)
    service = RatingsService(conn)

    service.rate(wid, 1)

    row = conn.execute("select rating, comment from ratings").fetchone()
    assert row["rating"] == 1
    assert service.rating_for(wid) == 1


def test_second_rating_replaces_the_first(tmp_path):
    conn, wid = _setup(tmp_path)
    service = RatingsService(conn)

    service.rate(wid, 1)
    service.rate(wid, -1, comment="太暗")

    rows = conn.execute("select rating, comment from ratings").fetchall()
    assert len(rows) == 1
    assert rows[0]["rating"] == -1
    assert rows[0]["comment"] == "太暗"


def test_rating_without_an_analysis_stores_empty_tags(tmp_path):
    conn, wid = _setup(tmp_path)
    RatingsService(conn).rate(wid, 1)

    assert conn.execute("select derived_tags_json from ratings").fetchone()[0] == "[]"


def test_rating_inherits_the_primary_style_tag(tmp_path):
    conn, wid = _setup(tmp_path)
    conn.execute(
        "insert into analyses(model,prompt_version,image_hash,scores_json,quality,"
        "creativity,novelty,formulaic,style_tags_json,created_at)"
        " values('m','v1','h','{}',80,70,60,10,'[\"landscape\",\"calm\"]','x')"
    )
    aid = conn.execute("select id from analyses").fetchone()[0]
    conn.execute(
        "insert into wallpaper_analyses(wallpaper_id,analysis_id,created_at) values(?,?,?)",
        (wid, aid, "x"),
    )
    conn.commit()

    RatingsService(conn).rate(wid, 1)

    assert conn.execute("select derived_tags_json from ratings").fetchone()[0] == '["landscape"]'


def test_comment_is_capped_at_200_characters(tmp_path):
    conn, wid = _setup(tmp_path)
    RatingsService(conn).rate(wid, 1, comment="字" * 300)

    assert len(conn.execute("select comment from ratings").fetchone()[0]) == 200


def test_rating_is_clamped_to_the_three_allowed_values(tmp_path):
    conn, wid = _setup(tmp_path)
    service = RatingsService(conn)

    with pytest.raises(ValueError):
        service.rate(wid, 7)


def test_history_returns_the_latest_decisions(tmp_path):
    conn, wid = _setup(tmp_path)
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-18',?,'70','80','74.5','r','applied','x')",
        (wid,),
    )
    conn.commit()
    RatingsService(conn).rate(wid, 1)

    rows = RatingsService(conn).history()

    assert len(rows) == 1
    assert rows[0].title == "测试壁纸"
    assert rows[0].rating == 1
    assert rows[0].final_score == pytest.approx(74.5)


def test_history_includes_unrated_entries(tmp_path):
    conn, wid = _setup(tmp_path)
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-18',?,'70','80','74.5','r','applied','x')",
        (wid,),
    )
    conn.commit()

    assert RatingsService(conn).history()[0].rating is None
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_ratings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dailywallpaper.recommend.ratings'`

- [ ] **Step 3: Implement**

```python
# src/dailywallpaper/recommend/ratings.py
"""User ratings and the history feed (spec 8.7, 11.2, 11.3)."""
from __future__ import annotations

import json
from dataclasses import dataclass

from dailywallpaper.db import now_iso

ALLOWED = (-1, 0, 1)
HISTORY_SQL = """
select d.wallpaper_id, w.title, w.local_path, d.decision_date, d.final_score,
       r.rating, r.comment
from decisions d
join wallpapers w on w.id = d.wallpaper_id
left join ratings r on r.wallpaper_id = d.wallpaper_id
order by d.decision_date desc, d.id desc
limit ?
"""


@dataclass(frozen=True)
class HistoryRow:
    wallpaper_id: int
    title: str
    local_path: str
    decision_date: str
    final_score: float
    rating: int | None
    comment: str


class RatingsService:
    def __init__(self, conn):
        self.conn = conn

    def _primary_tags(self, wallpaper_id: int) -> list[str]:
        row = self.conn.execute(
            """
            select a.style_tags_json
            from wallpaper_analyses wa join analyses a on a.id = wa.analysis_id
            where wa.wallpaper_id = ?
            order by a.id desc limit 1
            """,
            (wallpaper_id,),
        ).fetchone()
        if not row:
            return []
        tags = json.loads(row["style_tags_json"] or "[]")
        return tags[:1] if tags else []

    def rate(self, wallpaper_id: int, rating: int, comment: str = "", reasons=None) -> None:
        if rating not in ALLOWED:
            raise ValueError(f"rating must be one of {ALLOWED}")
        text = (comment or "").strip()[:200]
        if reasons:
            text = (text + " " + "、".join(reasons)).strip()[:200]
        self.conn.execute(
            """
            insert into ratings(wallpaper_id,rating,comment,derived_tags_json,created_at)
            values(?,?,?,?,?)
            on conflict(wallpaper_id) do update set
              rating=excluded.rating, comment=excluded.comment,
              derived_tags_json=excluded.derived_tags_json,
              created_at=excluded.created_at
            """,
            (
                wallpaper_id,
                rating,
                text,
                json.dumps(self._primary_tags(wallpaper_id), ensure_ascii=False),
                now_iso(),
            ),
        )
        self.conn.commit()

    def rating_for(self, wallpaper_id: int) -> int | None:
        row = self.conn.execute(
            "select rating from ratings where wallpaper_id=?", (wallpaper_id,)
        ).fetchone()
        return None if row is None else int(row[0])

    def history(self, days: int = 90, limit: int = 90) -> list[HistoryRow]:
        rows = self.conn.execute(HISTORY_SQL, (limit,)).fetchall()
        return [
            HistoryRow(
                wallpaper_id=row["wallpaper_id"],
                title=row["title"] or "",
                local_path=row["local_path"] or "",
                decision_date=row["decision_date"],
                final_score=float(row["final_score"]),
                rating=None if row["rating"] is None else int(row["rating"]),
                comment=row["comment"] or "",
            )
            for row in rows
        ]
```

- [ ] **Step 4: Run tests, then the full suite**

Run: `pytest tests/test_ratings.py -v` → all pass.
Run: `pytest -q` → all pass (194 + new).

- [ ] **Step 5: Commit**

```bash
git add src/dailywallpaper/recommend/ratings.py tests/test_ratings.py
git commit -m "feat: add the rating store and history feed"
```

---

### Task 2: 评分窗

**Files:**
- Create: `src/dailywallpaper/ui/rating_dialog.py`
- Test: `tests/test_ui_smoke.py`（追加）

**Interfaces:**
- Consumes: `RatingsService.rate`、`RatingsService.rating_for`
- Produces: `RatingDialog(service, wallpaper_id, parent=None)`，含 `.chosen`（保存后的评分或 None）、`QUICK_REASONS`

- [ ] **Step 1: Write the failing tests**

```python
def test_rating_dialog_saves_like_and_reason(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.rating_dialog import RatingDialog

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,created_at) values('t','1','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.commit()
    service = RatingsService(conn)

    dialog = RatingDialog(service, wid)
    dialog.reason_buttons["太暗"].click()
    dialog.like_button.click()

    assert dialog.chosen == 1
    row = conn.execute("select rating, comment from ratings").fetchone()
    assert row["rating"] == 1
    assert "太暗" in row["comment"]


def test_rating_dialog_exposes_the_documented_reasons(qapp, tmp_path):
    from dailywallpaper.ui.rating_dialog import QUICK_REASONS

    assert QUICK_REASONS == [
        "太花哨", "太暗", "不喜欢动漫", "想要风景", "想要暗色", "想要简约", "想要人物", "其他"
    ]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_ui_smoke.py -k rating_dialog -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dailywallpaper.ui.rating_dialog'`

- [ ] **Step 3: Implement**

```python
# src/dailywallpaper/ui/rating_dialog.py
"""Rating window (spec 11.3)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

QUICK_REASONS = [
    "太花哨", "太暗", "不喜欢动漫", "想要风景",
    "想要暗色", "想要简约", "想要人物", "其他",
]
COMMENT_MAX = 200


class RatingDialog(QDialog):
    def __init__(self, service, wallpaper_id, parent=None):
        super().__init__(parent)
        self.service = service
        self.wallpaper_id = wallpaper_id
        self.chosen = None
        self.setWindowTitle("给这张壁纸打分")

        self.like_button = QPushButton("喜欢")
        self.neutral_button = QPushButton("中性")
        self.dislike_button = QPushButton("不喜欢")
        self.like_button.clicked.connect(lambda: self._save(1))
        self.neutral_button.clicked.connect(lambda: self._save(0))
        self.dislike_button.clicked.connect(lambda: self._save(-1))

        self.reason_buttons = {}
        grid = QGridLayout()
        for index, reason in enumerate(QUICK_REASONS):
            button = QPushButton(reason)
            button.setCheckable(True)
            self.reason_buttons[reason] = button
            grid.addWidget(button, index // 4, index % 4)

        self.comment_edit = QLineEdit()
        self.comment_edit.setMaxLength(COMMENT_MAX)
        self.comment_edit.setPlaceholderText("补充一句（可选）")

        self.hint_label = QLabel("评分会立即影响明天的推荐")

        buttons = QHBoxLayout()
        buttons.addWidget(self.like_button)
        buttons.addWidget(self.neutral_button)
        buttons.addWidget(self.dislike_button)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("快捷理由"))
        layout.addLayout(grid)
        layout.addWidget(self.comment_edit)
        layout.addLayout(buttons)
        layout.addWidget(self.hint_label)

    def selected_reasons(self) -> list[str]:
        return [reason for reason, button in self.reason_buttons.items() if button.isChecked()]

    def _save(self, rating: int) -> None:
        self.service.rate(
            self.wallpaper_id,
            rating,
            comment=self.comment_edit.text(),
            reasons=self.selected_reasons(),
        )
        self.chosen = rating
        self.accept()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ui_smoke.py -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dailywallpaper/ui/rating_dialog.py tests/test_ui_smoke.py
git commit -m "feat: add the rating dialog"
```

---

### Task 3: 历史页 + 标签页容器

**Files:**
- Create: `src/dailywallpaper/ui/history_page.py`
- Modify: `src/dailywallpaper/ui/main_window.py`（包一层 `QTabWidget`）
- Test: `tests/test_ui_smoke.py`（追加）

**Interfaces:**
- Consumes: `RatingsService.history() -> list[HistoryRow]`、`CommandService.undo()`
- Produces: `HistoryPage(ratings, on_undo, parent=None)`，含 `.refresh()`、`.row_count`、`MainWindow.tabs`

- [ ] **Step 1: Write the failing tests**

```python
def test_history_page_lists_recent_decisions(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.recommend.ratings import RatingsService
    from dailywallpaper.ui.history_page import HistoryPage

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,title,created_at)"
                 " values('bing','1','历史图','x')")
    wid = conn.execute("select id from wallpapers").fetchone()[0]
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,"
        "personalize_score,final_score,reason,status,created_at)"
        " values('2026-10-17',?,'70','80','74.5','r','applied','x')", (wid,))
    conn.commit()

    page = HistoryPage(RatingsService(conn), on_undo=lambda: True)
    page.refresh()

    assert page.row_count == 1
    assert qapp is not None


def test_main_window_has_today_and_history_tabs(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    window = MainWindow(_Service(), conn)

    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["今日", "历史"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_ui_smoke.py -k "history_page or tabs" -v`
Expected: FAIL — module missing / no `tabs`.

- [ ] **Step 3: Implement**

```python
# src/dailywallpaper/ui/history_page.py
"""Recent 90 days with ratings and undo (spec 11.2)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

THUMB = 96
HEADERS = ["日期", "缩略图", "标题", "总分", "我的评分"]


class HistoryPage(QWidget):
    def __init__(self, ratings, on_undo, parent=None):
        super().__init__(parent)
        self.ratings = ratings
        self.on_undo = on_undo
        self.row_count = 0

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setRowHeight(0, THUMB)
        self.undo_button = QPushButton("撤销上一张")
        self.undo_button.clicked.connect(lambda: self.on_undo())
        self.status = QLabel("")

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addWidget(self.undo_button)
        layout.addWidget(self.status)
        self.refresh()

    def refresh(self) -> None:
        rows = self.ratings.history()
        self.row_count = len(rows)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(row.decision_date))
            label = QLabel()
            pixmap = QPixmap(row.local_path) if row.local_path else QPixmap()
            if not pixmap.isNull():
                label.setPixmap(pixmap.scaled(THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio))
            self.table.setCellWidget(index, 1, label)
            self.table.setItem(index, 2, QTableWidgetItem(row.title))
            self.table.setItem(index, 3, QTableWidgetItem(f"{row.final_score:.1f}"))
            text = {1: "喜欢", 0: "中性", -1: "不喜欢"}.get(row.rating, "")
            self.table.setItem(index, 4, QTableWidgetItem(text))
```

`main_window.py` 改为：构造函数里建 `self.tabs = QTabWidget()`，`self.today = QWidget()` 承载现有控件，`self.history = HistoryPage(RatingsService(conn), on_undo=self._undo)`，`self.tabs.addTab(self.today, "今日")`、`self.tabs.addTab(self.history, "历史")`，`refresh()` 同时刷新历史页。

- [ ] **Step 4: Run tests, then the full suite**

Run: `pytest tests/test_ui_smoke.py -q` → all pass.
Run: `pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dailywallpaper/ui/history_page.py src/dailywallpaper/ui/main_window.py tests/test_ui_smoke.py
git commit -m "feat: add the history tab"
```

---

### Task 4: 接线（托盘 + 今日页按钮）

**Files:**
- Modify: `src/dailywallpaper/ui/tray.py`、`src/dailywallpaper/ui/main_window.py`、`src/dailywallpaper/ui/shell.py`
- Test: `tests/test_ui_smoke.py`（追加）

**Interfaces:**
- Consumes: `RatingDialog`、`RatingsService`
- Produces: 托盘的"喜欢/不喜欢"打开评分窗；今日页三颗按钮复用同一个对话框

- [ ] **Step 1: Write the failing test**

```python
def test_tray_exposes_like_and_dislike(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)

    labels = [action.text() for action in tray.menu.actions() if action.text()]

    assert "喜欢" in labels
    assert "不喜欢" in labels
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_ui_smoke.py -k like_and_dislike -v`
Expected: FAIL — the labels are absent.

- [ ] **Step 3: Implement**

在 `TrayIcon.__init__` 增加 `on_rate` 参数（默认 `None`），并插入两项：

```python
        self.like_action = QAction("喜欢", self.menu)
        self.like_action.triggered.connect(lambda: on_rate(1) if on_rate else None)
        self.dislike_action = QAction("不喜欢", self.menu)
        self.dislike_action.triggered.connect(lambda: on_rate(-1) if on_rate else None)
```

放在 `undo_action` 之后、`pause_action` 之前。`UiShell` 传入 `on_rate=self.show_rating`，`show_rating` 打开 `RatingDialog`。今日页的三颗按钮调用同一个回调。

- [ ] **Step 4: Run the full suite**

Run: `pytest -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dailywallpaper/ui tests/test_ui_smoke.py
git commit -m "feat: wire ratings into the tray and today tab"
```

---

## 后续

- **1B-3**：成本页、设置页、开机自启。
- **1B-4**：`glm-4.6v` 付费复核前 5 名。
- 1B-1 遗留的 7 条 Minor 见 `.superpowers/sdd/2026-10-18-dailywallpaper-phase1b-ui-shell/progress.md`。
