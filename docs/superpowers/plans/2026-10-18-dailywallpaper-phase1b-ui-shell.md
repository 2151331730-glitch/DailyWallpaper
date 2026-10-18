# DailyWallpaper Phase 1B-1（GUI 外壳）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给已经能跑的 headless 核心加一层 PySide6 桌面外壳：托盘、今日页主窗、暂停/换一张/撤销命令，并让 `python -m dailywallpaper` 默认启动 GUI。

**Architecture:** 所有"做事"的逻辑放进一个可测试的 `CommandService`（不依赖 Qt），Qt 层只负责显示状态和转发点击。UI 通过 `QTimer` 每 2 秒轮询 `CommandService.snapshot()`；后台 tick 在 `QThread` 里跑，绝不阻塞界面。本计划只做 spec 11.1 的托盘菜单 + 11.2 的"今日"标签页；历史/成本/设置标签页、评分窗、付费复核模型属于后续计划。

**Tech Stack:** Python 3.13、PySide6 6.12、SQLite、pytest（Qt 用 offscreen 平台测试）。

**Spec:** `docs/superpowers/specs/2026-10-17-dailywallpaper-design.md`（第 7、9.3、11 节是本次的权威依据）

## Global Constraints

- 不新增第三方依赖：只用 `PySide6`（已在 `pyproject.toml`）。禁止 pytest-qt、qt-material 等。
- 不在更换壁纸时弹窗；成功只发一个 5 秒自动消失的托盘气泡。
- UI 线程绝不做网络/模型/文件 IO；后台线程负责 tick。
- 每日最多成功一次由 `change_log` 唯一索引保证，UI 只负责在按钮上体现语义。
- 失败不弹窗；原因写进主窗状态区与日志。
- 所有新增代码走 TDD：先写失败测试，再实现，再跑全量。
- 测试里 Qt 一律用 `QT_QPA_PLATFORM=offscreen`，不允许真弹窗。
- 提交信息用 `feat:` / `fix:` / `test:` 前缀，与仓库既有风格一致。

## Review Focus

1. 用户在托盘连点"换一张"两次：必须只应用一次（共享串行锁）。
2. 当天已自动换过时点"换一张"：默认禁用；开启覆盖时要提示"本次为手动覆盖"。
3. 暂停期间点"换一张"：手动仍可执行，只是自动不再触发。
4. 后台 tick 抛异常：托盘状态变"错误"，进程不退。
5. 关闭主窗（点 ×）：只隐藏到托盘，不退出；只有"退出"才退出。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `src/dailywallpaper/ui/__init__.py` | 空包 |
| `src/dailywallpaper/ui/commands.py` | `CommandService`：暂停/恢复/换一张/撤销/当前快照。**不 import Qt** |
| `src/dailywallpaper/ui/tray.py` | `TrayIcon`：图标状态、菜单、气泡 |
| `src/dailywallpaper/ui/main_window.py` | `MainWindow`：今日页（预览、分数、理由、按钮） |
| `src/dailywallpaper/ui/shell.py` | `UiShell`：把 commands/tray/window/后台线程接起来 |
| `src/dailywallpaper/__main__.py` | 加 `--headless`；默认启动 GUI |
| `tests/test_commands.py` | CommandService 的行为测试 |
| `tests/test_ui_smoke.py` | offscreen 下构造托盘/主窗/外壳的冒烟测试 |

---

### Task 1: CommandService（无 Qt）

**Files:**
- Create: `src/dailywallpaper/ui/__init__.py`
- Create: `src/dailywallpaper/ui/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `DailyStateMachine.tick(dry_run=False) -> TickResult`、`ApplyService.undo_last() -> bool`、`conn`、`config`、`clock`
- Produces:
  - `CommandService(conn, config, clock, machine, apply_service)`
  - `.pause(days: int | None) -> None`（`None` = 直到手动恢复）
  - `.resume() -> None`
  - `.is_paused() -> bool`
  - `.can_change_manually() -> bool`
  - `.change_now() -> TickResult`
  - `.undo() -> bool`
  - `.snapshot() -> UiState`，`UiState(today_changed: bool, paused: bool, can_change: bool, last_action: str, last_reason: str)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_commands.py
from datetime import datetime, timedelta

from dailywallpaper.config import AppSection
from dailywallpaper.db import connect, migrate, now_iso
from dailywallpaper.ui.commands import CommandService
from dailywallpaper.state.machine import TickResult


class _Clock:
    def __init__(self, moment):
        self.moment = moment

    def now(self):
        return self.moment


class _Machine:
    def __init__(self):
        self.calls = []

    def tick(self, dry_run=False):
        self.calls.append(dry_run)
        return TickResult("applied", "", 1)


class _ApplyService:
    def __init__(self, ok=True):
        self.ok = ok
        self.undos = 0

    def undo_last(self):
        self.undos += 1
        return self.ok


def _setup(tmp_path, moment=datetime(2026, 10, 18, 9, 0)):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    machine = _Machine()
    service = CommandService(conn, AppSection(), _Clock(moment), machine, _ApplyService())
    return conn, machine, service


def test_not_paused_by_default(tmp_path):
    conn, machine, service = _setup(tmp_path)
    assert service.is_paused() is False
    assert service.snapshot().paused is False


def test_pause_for_one_day(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.pause(1)
    assert service.is_paused() is True
    row = conn.execute("select value from state where key='paused_until'").fetchone()
    assert row[0].startswith("2026-10-19")


def test_pause_forever_stores_a_far_future_date(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.pause(None)
    row = conn.execute("select value from state where key='paused_until'").fetchone()
    assert int(row[0][:4]) >= 2099


def test_resume_clears_the_pause(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.pause(7)
    service.resume()
    assert service.is_paused() is False


def test_expired_pause_is_not_paused(tmp_path):
    conn, machine, service = _setup(tmp_path)
    conn.execute("insert into state(key,value) values('paused_until','2026-10-01T00:00:00')")
    conn.commit()
    assert service.is_paused() is False


def test_manual_change_is_allowed_when_nothing_changed_today(tmp_path):
    conn, machine, service = _setup(tmp_path)
    assert service.can_change_manually() is True


def test_manual_change_is_blocked_after_a_success_today(tmp_path):
    conn, machine, service = _setup(tmp_path)
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,method,applied_at,success)"
        " values('2026-10-18',1,'static','x',1)"
    )
    conn.commit()
    assert service.can_change_manually() is False


def test_override_setting_allows_a_second_change(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert into change_log(change_date,wallpaper_id,method,applied_at,success)"
        " values('2026-10-18',1,'static','x',1)"
    )
    conn.commit()
    config = AppSection(allow_manual_override_after_daily_change=True)
    service = CommandService(conn, config, _Clock(datetime(2026, 10, 18, 9, 0)), _Machine(), _ApplyService())
    assert service.can_change_manually() is True


def test_change_now_asks_the_machine_for_a_real_apply(tmp_path):
    conn, machine, service = _setup(tmp_path)
    result = service.change_now()
    assert result.action == "applied"
    assert machine.calls == [False]


def test_snapshot_reports_the_last_action(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.change_now()
    snapshot = service.snapshot()
    assert snapshot.last_action == "applied"
    assert snapshot.today_changed is False  # the fake machine writes no change_log row


def test_undo_delegates_to_the_apply_service(tmp_path):
    conn, machine, service = _setup(tmp_path)
    assert service.undo() is True


def test_double_click_applies_only_once(tmp_path):
    import threading

    conn, machine, service = _setup(tmp_path)
    results = []

    def click():
        results.append(service.change_now())

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(machine.calls) == 1
    assert len([result for result in results if result is not None]) == 1


def test_manual_change_still_works_while_paused(tmp_path):
    conn, machine, service = _setup(tmp_path)
    service.pause(1)

    service.change_now()

    assert machine.calls == [False]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dailywallpaper.ui'`

- [ ] **Step 3: Implement**

```python
# src/dailywallpaper/ui/__init__.py
"""PySide6 desktop shell."""
```

```python
# src/dailywallpaper/ui/commands.py
"""Everything the UI can ask for, with no Qt import."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta

PAUSE_KEY = "paused_until"
FOREVER_YEARS = 100


@dataclass(frozen=True)
class UiState:
    today_changed: bool
    paused: bool
    can_change: bool
    last_action: str = ""
    last_reason: str = ""


class CommandService:
    def __init__(self, conn, config, clock, machine, apply_service):
        self.conn = conn
        self.config = config
        self.clock = clock
        self.machine = machine
        self.apply_service = apply_service
        self._last_action = ""
        self._last_reason = ""
        # A double click in the tray must not apply twice (Review Focus 1).
        self._apply_lock = threading.Lock()

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def is_paused(self) -> bool:
        row = self.conn.execute(
            "select value from state where key=?", (PAUSE_KEY,)
        ).fetchone()
        if not row:
            return False
        from dailywallpaper.db import _as_aware, parse_ts

        try:
            return _as_aware(self.clock.now()) < parse_ts(row[0])
        except (ValueError, TypeError):
            return False

    def pause(self, days: int | None) -> None:
        until = self.clock.now() + timedelta(days=days if days else FOREVER_YEARS * 365)
        self.conn.execute(
            "insert or replace into state(key,value) values(?,?)",
            (PAUSE_KEY, until.isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def resume(self) -> None:
        self.conn.execute("delete from state where key=?", (PAUSE_KEY,))
        self.conn.commit()

    def _changed_today(self) -> bool:
        if self.config.manual_change_counts_toward_daily_limit:
            sql = "select 1 from change_log where change_date=? and success=1"
        else:
            sql = ("select 1 from change_log where change_date=? and success=1"
                   " and method<>'manual'")
        return self.conn.execute(sql, (self._today(),)).fetchone() is not None

    def can_change_manually(self) -> bool:
        if not self._changed_today():
            return True
        return bool(self.config.allow_manual_override_after_daily_change)

    def change_now(self):
        if not self._apply_lock.acquire(blocking=False):
            return None  # a change is already running; ignore the extra click
        try:
            result = self.machine.tick(dry_run=False)
            self._last_action = result.action
            self._last_reason = result.reason
            return result
        finally:
            self._apply_lock.release()

    def undo(self) -> bool:
        return bool(self.apply_service.undo_last())

    def snapshot(self) -> UiState:
        return UiState(
            today_changed=self._changed_today(),
            paused=self.is_paused(),
            can_change=self.can_change_manually(),
            last_action=self._last_action,
            last_reason=self._last_reason,
        )
```

- [ ] **Step 4: Run to verify pass, then the full suite**

Run: `.venv\Scripts\python -m pytest tests/test_commands.py -v`
Expected: all pass.
Run: `.venv\Scripts\python -m pytest -q`
Expected: all pass (163 + new).

- [ ] **Step 5: Commit**

```bash
git add src/dailywallpaper/ui tests/test_commands.py
git commit -m "feat: add Qt-free command service for the desktop shell"
```

---

### Task 2: 托盘图标与菜单

**Files:**
- Create: `src/dailywallpaper/ui/tray.py`
- Test: `tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: `CommandService.snapshot() -> UiState`、`.pause(days)`、`.resume()`、`.change_now()`、`.undo()`
- Produces: `TrayIcon(service, on_open, on_quit, parent=None)`，含 `.set_state(kind: str)`（`idle`/`busy`/`changed`/`error`）、`.notify(title, message)`

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_ui_smoke.py
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtWidgets import QApplication

from dailywallpaper.ui.tray import TrayIcon


class _Service:
    def __init__(self):
        self.paused = False
        self.pauses = []

    def snapshot(self):
        from dailywallpaper.ui.commands import UiState
        return UiState(today_changed=False, paused=self.paused, can_change=True)

    def pause(self, days):
        self.pauses.append(days)
        self.paused = True

    def resume(self):
        self.paused = False

    def change_now(self):
        from dailywallpaper.state.machine import TickResult
        self.changed = True
        return TickResult("applied")

    def undo(self):
        return True


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_tray_has_the_documented_menu(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)
    labels = [action.text() for action in tray.menu.actions() if action.text()]
    assert labels[:6] == ["今日壁纸", "换一张", "撤销上一张", "暂停自动更换", "设置", "退出"]


def test_pause_menu_offers_day_week_forever(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)
    labels = [a.text() for a in tray.pause_menu.actions()]
    assert labels == ["1 天", "7 天", "直到手动恢复"]


def test_pause_action_calls_the_service(qapp):
    service = _Service()
    tray = TrayIcon(service, on_open=lambda: None, on_quit=lambda: None)
    tray.pause_menu.actions()[1].trigger()
    assert service.pauses == [7]


def test_change_action_is_disabled_when_today_changed(qapp):
    class _Blocked(_Service):
        def snapshot(self):
            from dailywallpaper.ui.commands import UiState
            return UiState(today_changed=True, paused=False, can_change=False)

    tray = TrayIcon(_Blocked(), on_open=lambda: None, on_quit=lambda: None)
    tray.refresh()
    assert tray.change_action.isEnabled() is False


def test_state_switch_does_not_crash(qapp):
    tray = TrayIcon(_Service(), on_open=lambda: None, on_quit=lambda: None)
    for kind in ("idle", "busy", "changed", "error"):
        tray.set_state(kind)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_ui_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dailywallpaper.ui.tray'`

- [ ] **Step 3: Implement**

```python
# src/dailywallpaper/ui/tray.py
"""System tray icon and menu (spec 11.1)."""
from __future__ import annotations

from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

STATE_COLOURS = {
    "idle": "#6b7280",
    "busy": "#2563eb",
    "changed": "#16a34a",
    "error": "#dc2626",
}


def _dot(colour: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setBrush(QColor(colour))
    painter.setPen(QColor(colour))
    painter.drawEllipse(4, 4, 24, 24)
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    def __init__(self, service, on_open, on_quit, parent=None):
        super().__init__(_dot(STATE_COLOURS["idle"]), parent)
        self.service = service
        self.on_open = on_open
        self.on_quit = on_quit

        self.menu = QMenu()
        self.today_action = QAction("今日壁纸", self.menu)
        self.today_action.triggered.connect(lambda: self.on_open())
        self.change_action = QAction("换一张", self.menu)
        self.change_action.triggered.connect(self._change_now)
        self.undo_action = QAction("撤销上一张", self.menu)
        self.undo_action.triggered.connect(lambda: self.service.undo())
        self.pause_action = QAction("暂停自动更换", self.menu)
        self.pause_menu = QMenu("暂停自动更换", self.menu)
        for label, days in (("1 天", 1), ("7 天", 7), ("直到手动恢复", None)):
            action = QAction(label, self.pause_menu)
            action.triggered.connect(lambda _checked=False, d=days: self._pause(d))
            self.pause_menu.addAction(action)
        self.pause_action.setMenu(self.pause_menu)
        self.settings_action = QAction("设置", self.menu)
        self.quit_action = QAction("退出", self.menu)
        self.quit_action.triggered.connect(lambda: self.on_quit())

        for action in (
            self.today_action,
            self.change_action,
            self.undo_action,
            self.pause_action,
            self.settings_action,
        ):
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.setContextMenu(self.menu)
        self.refresh()

    def _change_now(self):
        self.service.change_now()
        self.refresh()

    def _pause(self, days):
        if days is None:
            self.service.pause(None)
        else:
            self.service.pause(days)
        self.refresh()

    def refresh(self) -> None:
        snapshot = self.service.snapshot()
        self.change_action.setEnabled(snapshot.can_change)
        label = "恢复自动更换" if snapshot.paused else "暂停自动更换"
        self.pause_action.setText(label)
        self.pause_menu.setEnabled(not snapshot.paused)

    def set_state(self, kind: str) -> None:
        self.setIcon(_dot(STATE_COLOURS.get(kind, STATE_COLOURS["idle"])))

    def notify(self, title: str, message: str) -> None:
        self.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)
```

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python -m pytest tests/test_ui_smoke.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dailywallpaper/ui/tray.py tests/test_ui_smoke.py
git commit -m "feat: add system tray with pause, change and undo"
```

---

### Task 3: 今日页主窗

**Files:**
- Create: `src/dailywallpaper/ui/main_window.py`
- Test: `tests/test_ui_smoke.py`（追加）

**Interfaces:**
- Consumes: `CommandService`、`conn`（读取最近一条 `decisions` + `wallpapers` + `analyses`）
- Produces: `MainWindow(service, conn, parent=None)`，含 `.refresh()`、重写 `.closeEvent` 只隐藏

- [ ] **Step 1: Write the failing test**

```python
def test_main_window_shows_the_latest_decision(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute("insert into wallpapers(source,source_id,title,created_at)"
                 " values('bing','1','测试壁纸','x')")
    conn.execute(
        "insert into decisions(decision_date,wallpaper_id,explore_score,personalize_score,"
        "final_score,reason,status,created_at) values('2026-10-18',1,70,80,74.5,'reason','applied','x')"
    )
    conn.commit()

    window = MainWindow(_Service(), conn)
    window.refresh()

    assert "测试壁纸" in window.title_label.text()
    assert "74.5" in window.score_label.text()


def test_closing_the_window_hides_it(qapp, tmp_path):
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.ui.main_window import MainWindow

    conn = connect(tmp_path / "app.db")
    migrate(conn)
    window = MainWindow(_Service(), conn)
    window.show()
    window.close()
    assert window.isVisible() is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_ui_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dailywallpaper.ui.main_window'`

- [ ] **Step 3: Implement**

```python
# src/dailywallpaper/ui/main_window.py
"""Today tab: current wallpaper, scores and the three actions (spec 11.2)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

LATEST_SQL = """
select w.title, w.source, w.local_path, d.explore_score, d.personalize_score,
       d.final_score, d.reason
from decisions d join wallpapers w on w.id = d.wallpaper_id
order by d.id desc limit 1
"""


class MainWindow(QWidget):
    def __init__(self, service, conn, parent=None):
        super().__init__(parent)
        self.service = service
        self.conn = conn
        self.setWindowTitle("每日壁纸")
        self.resize(520, 620)

        self.preview_label = QLabel("还没有更换记录")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(280)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.meta_label = QLabel("")
        self.score_label = QLabel("")
        self.reason_label = QLabel("")
        self.reason_label.setWordWrap(True)

        self.change_button = QPushButton("换一张")
        self.change_button.clicked.connect(self._change_now)
        self.undo_button = QPushButton("撤销上一张")
        self.undo_button.clicked.connect(self._undo)

        actions = QHBoxLayout()
        actions.addWidget(self.change_button)
        actions.addWidget(self.undo_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preview_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.meta_label)
        layout.addWidget(self.score_label)
        layout.addWidget(self.reason_label)
        layout.addLayout(actions)
        self.refresh()

    def _change_now(self):
        self.service.change_now()
        self.refresh()

    def _undo(self):
        self.service.undo()
        self.refresh()

    def refresh(self) -> None:
        row = self.conn.execute(LATEST_SQL).fetchone()
        if not row:
            self.title_label.setText("")
            self.meta_label.setText("")
            self.score_label.setText("")
            self.reason_label.setText("")
        else:
            self.title_label.setText(row["title"] or "（无标题）")
            self.meta_label.setText(f"来源：{row['source']}")
            self.score_label.setText(
                f"E {row['explore_score']:.1f} · P {row['personalize_score']:.1f}"
                f" · 总分 {row['final_score']:.1f}"
            )
            self.reason_label.setText(row["reason"] or "")
            path = row["local_path"]
            if path:
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    self.preview_label.setPixmap(
                        pixmap.scaled(
                            self.preview_label.width(),
                            280,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
        snapshot = self.service.snapshot()
        self.change_button.setEnabled(snapshot.can_change)

    def closeEvent(self, event):
        # Closing the window only hides it; quitting goes through the tray.
        self.hide()
        event.ignore()
```

- [ ] **Step 4: Run tests**

Run: `.venv\Scripts\python -m pytest tests/test_ui_smoke.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/dailywallpaper/ui/main_window.py tests/test_ui_smoke.py
git commit -m "feat: add the today tab"
```

---

### Task 4: 外壳 + CLI 启动 GUI

**Files:**
- Create: `src/dailywallpaper/ui/shell.py`
- Modify: `src/dailywallpaper/__main__.py`
- Test: `tests/test_ui_smoke.py`（追加）、`tests/test_cli.py`（追加）

**Interfaces:**
- Consumes: `build_application(cfg)` 的 `Application.machine`、`CommandService`、`TrayIcon`、`MainWindow`
- Produces: `UiShell(config, command_service, on_quit)`，含 `.start()`；`main(..., gui=True)`

- [ ] **Step 1: Write the failing tests**

```python
def test_shell_wires_tray_and_window(qapp):
    from dailywallpaper.ui.shell import UiShell

    shell = UiShell(_Service(), _Service(), on_quit=lambda: None)
    assert shell.tray is not None
    assert shell.window is not None


def test_cli_headless_flag_skips_the_gui(tmp_path):
    from dailywallpaper.__main__ import main

    calls = []
    cfg = _config(tmp_path)

    class _App:
        def run_once(self, dry_run=False):
            calls.append(("once", dry_run))
            return 0

        def run_forever(self):
            calls.append(("forever", None))
            return 0

    main(["--config", str(cfg), "--once", "--headless"], app_factory=lambda c: _App())

    assert calls == [("once", False)]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_ui_smoke.py tests/test_cli.py -v`
Expected: FAIL — `No module named 'dailywallpaper.ui.shell'`, and `--headless` rejected by argparse.

- [ ] **Step 3: Implement**

```python
# src/dailywallpaper/ui/shell.py
"""Wire the tray, the window and the background ticker together."""
from __future__ import annotations

from PySide6.QtCore import QTimer

from dailywallpaper.ui.main_window import MainWindow
from dailywallpaper.ui.tray import TrayIcon


class UiShell:
    def __init__(self, command_service, machine, on_quit, parent=None):
        self.service = command_service
        self.machine = machine
        self.on_quit = on_quit
        self.window = MainWindow(command_service, command_service.conn)
        self.tray = TrayIcon(
            command_service,
            on_open=self.show_window,
            on_quit=self._quit,
        )
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(2000)

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()

    def _poll(self) -> None:
        self.window.refresh()
        self.tray.refresh()

    def _quit(self) -> None:
        self.tray.hide()
        self.on_quit()

    def start(self) -> None:
        self.tray.show()
```

`__main__.py` 改为：

```python
def main(argv=None, app_factory=None, run_gui=None) -> int:
    parser = argparse.ArgumentParser(prog="dailywallpaper")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg.app.data_dir, cfg.app.log_level)

    if app_factory is None:
        from dailywallpaper.app import build_application

        app_factory = build_application
    application = app_factory(cfg)

    if args.once or args.headless:
        if args.once:
            return application.run_once(dry_run=args.dry_run)
        return application.run_forever()

    if run_gui is None:
        from dailywallpaper.ui.shell import build_gui
        run_gui = build_gui
    return run_gui(cfg)
```

`__main__.py` 顶部同时加：

```python
from dailywallpaper.app import SingleInstance
```

（已存在，勿重复导入。）
```

并在 `ui/shell.py` 追加：

```python
def build_gui(cfg) -> int:
    """Composition root for the desktop shell."""
    import sys

    from PySide6.QtWidgets import QApplication

    from dailywallpaper.app import SingleInstance, build_application
    from dailywallpaper.app import Application as _App
    from dailywallpaper.state.machine import SystemClock
    from dailywallpaper.ui.commands import CommandService

    application = _AppContext(cfg)
    app = QApplication.instance() or QApplication(sys.argv)
    shell = UiShell(application.service, application.machine, on_quit=app.quit)
    shell.start()
    return app.exec()


class _AppContext:
    def __init__(self, cfg):
        from dailywallpaper.app import build_application

        built = build_application(cfg)
        self.machine = built.machine
        conn = self.machine.conn
        from dailywallpaper.state.machine import SystemClock
        from dailywallpaper.ui.commands import CommandService

        self.service = CommandService(conn, cfg.app, SystemClock(), self.machine,
                                      self.machine.apply_service)
```

- [ ] **Step 4: Run the full suite**

Run: `.venv\Scripts\python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Run the app once by hand (offscreen smoke)**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python -c "import dailywallpaper.ui.shell as s; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add src/dailywallpaper/ui/shell.py src/dailywallpaper/__main__.py tests
git commit -m "feat: launch the desktop shell by default"
```

---

## 后续计划（不在本计划内）

- **1B-2 评分与历史**：评分窗（spec 11.3）、历史标签页、`ratings` 写入与 `PreferenceProfile` 联动。
- **1B-3 成本与设置**：成本标签页、设置标签页、开机自启、托盘气泡文案。
- **1B-4 付费复核**：`glm-4.6v` 复核前 5 名，写 `analyses`/`api_usage`，并在 UI 显示"复核"标记。
- **遗留修复**：Phase 1A 审查留下的 9 条 Minor（见 `.superpowers/sdd/…/progress.md`）。
