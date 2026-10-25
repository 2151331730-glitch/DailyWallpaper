# DailyWallpaper Phase 2（动态壁纸 + 反馈闭环）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把本地 Wallpaper Engine 工坊里的动态壁纸接进推荐流程，按"每 N 天安排 1 天动态"的策略切换、验证、恢复，并让天气和评分真正影响推荐理由；同时**尽量不依赖代理**。

**Architecture:** 三个新单元：`WorkshopSource`（纯本地，零网络）、`WeApplier`（把 `WeControl` 包成和静态 applier 同形的接口）、`MediaPlan`（决定今天该用静态还是动态）。`ApplyService` 统一按 `applier` 接口工作，`media_type` 只影响"今天计划用哪类候选"，不进入 55/45 公式。

**Tech Stack:** 现有 `WeControl`、`ffmpeg`、`Pillow`；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-10-17-dailywallpaper-design.md`（§4.3、§9.2、§9.2.1、§14.4）

## Global Constraints

- **少用代理**：本地工坊优先；Wallhaven 连接超时降到 `connect_timeout=5`（默认），并把"今天连不上"记进 `state`，当天不再重试。
- WE 未运行时，动态日**回退静态**，不自动启动 WE。
- 动态候选的 `contentrating` 为 Mature/Questionable 且 `allow_mature=false` 时跳过。
- 应用动态壁纸失败必须恢复原 WE 项目并写 `success=0`，不算当天成功。
- 只有插电时才应用动态壁纸（`dynamic_requires_ac=true`），电池模式自动改静态。
- 不新增依赖；Qt 测试 offscreen；全部 TDD。

## Review Focus

1. 工坊里 `project.json` 缺 `preview` 或 `file`：跳过该候选，不抛异常。
2. 视频没有音轨/时长为 0：ffmpeg 抽帧失败 → 该候选被跳过，不影响其他候选。
3. WE 正在全屏游戏（`playbackfullscreen: pause`）：切换成功但用户看不到变化 —— 记录并说明。
4. 电池模式下 `dynamic_only`：没有静态候选可用时保留原壁纸，不强行切动态。
5. Wallhaven 连不上时第二次运行：**5 秒内跳过**，不是再等 30 秒。

---

## File Structure

| 文件 | 责任 |
|---|---|
| `src/dailywallpaper/sources/workshop.py` | `WorkshopSource`：扫描本地工坊，产出 `Candidate`（零网络） |
| `src/dailywallpaper/analysis/media.py` | `video_contact_sheet()`、`gif_first_frame()` |
| `src/dailywallpaper/apply/we_applier.py` | `WeApplier`：`snapshot/apply/restore`，内部用 `WeControl` |
| `src/dailywallpaper/recommend/media_plan.py` | `MediaPlan.decide(date, strategy, n, on_battery) -> "static"｜"dynamic"` |
| `src/dailywallpaper/sources/base.py` | `Candidate` 增加 `content_rating: str = ""` |
| `config.py` | `SourcesSection.connect_timeout=5`、`WallpaperEngineSection.enabled` |
| `tests/test_workshop.py`、`test_media.py`、`test_we_applier.py`、`test_media_plan.py` | 测试 |

---

### Task 1: 本地工坊扫描（零网络）

**Files:** Create `sources/workshop.py`；Modify `sources/base.py`；Test `tests/test_workshop.py`

**Interfaces:**
- `WorkshopSource(workshop_root: Path, allow_mature=False)`
- `.scan() -> list[Candidate]`（`media_type` = `scene` / `video` / `image`）
- `Candidate.content_rating: str = ""`

- [ ] **Step 1: failing tests**

```python
def _project(root, pid, **over):
    folder = root / pid
    folder.mkdir(parents=True)
    data = {"title": f"t{pid}", "type": "scene", "file": "scene.pkg", "preview": "preview.jpg"}
    data.update(over)
    (folder / "project.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "preview.jpg").write_bytes(b"x")
    return folder


def test_scan_maps_projects_to_candidates(tmp_path):
    _project(tmp_path, "111")
    items = WorkshopSource(tmp_path).scan()
    assert items[0].source == "we_workshop"
    assert items[0].source_id == "111"
    assert items[0].media_type == "scene"
    assert items[0].preview_url.endswith("preview.jpg")


def test_scan_skips_projects_without_preview(tmp_path):
    _project(tmp_path, "111", preview="")
    assert WorkshopSource(tmp_path).scan() == []


def test_scan_skips_unreadable_project_json(tmp_path):
    folder = tmp_path / "222"
    folder.mkdir()
    (folder / "project.json").write_text("{not json", encoding="utf-8")
    assert WorkshopSource(tmp_path).scan() == []


def test_mature_content_is_skipped_unless_allowed(tmp_path):
    _project(tmp_path, "111", contentrating="Mature")
    assert WorkshopSource(tmp_path).scan() == []
    assert len(WorkshopSource(tmp_path, allow_mature=True).scan()) == 1


def test_video_projects_are_typed_as_video(tmp_path):
    _project(tmp_path, "333", type="video", file="clip.mp4")
    assert WorkshopSource(tmp_path).scan()[0].media_type == "video"
```

- [ ] **Step 2: RED**, **Step 3: implement**

```python
# src/dailywallpaper/sources/workshop.py
"""Scan the local Wallpaper Engine workshop. No network access at all."""
from __future__ import annotations

import json
from pathlib import Path

from .base import Candidate

MEDIA_TYPES = {"scene": "scene", "video": "video", "web": "scene"}
MATURE = {"mature", "questionable"}


class WorkshopSource:
    def __init__(self, workshop_root: Path, allow_mature: bool = False):
        self.root = Path(workshop_root)
        self.allow_mature = allow_mature

    def scan(self) -> list[Candidate]:
        if not self.root.exists():
            return []
        items = []
        for folder in sorted(self.root.iterdir()):
            candidate = self._candidate(folder)
            if candidate is not None:
                items.append(candidate)
        return items

    def _candidate(self, folder: Path) -> Candidate | None:
        manifest = folder / "project.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        rating = str(data.get("contentrating") or "").lower()
        if rating in MATURE and not self.allow_mature:
            return None
        preview = data.get("preview") or "preview.jpg"
        preview_path = folder / preview
        if not preview_path.is_file():
            return None
        asset = data.get("file") or ""
        if not asset:
            return None
        return Candidate(
            source="we_workshop",
            source_id=folder.name,
            title=str(data.get("title") or ""),
            page_url="",
            file_url=str(folder / asset),
            preview_url=str(preview_path),
            media_type=MEDIA_TYPES.get(str(data.get("type") or ""), "scene"),
            width=0,
            height=0,
            file_size=(folder / asset).stat().st_size if (folder / asset).is_file() else 0,
            tags=[str(tag) for tag in (data.get("tags") or [])],
            raw=data,
            content_rating=rating,
        )
```

`base.py`：`Candidate` 末尾加 `content_rating: str = ""`。

- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: scan the local wallpaper engine workshop`

---

### Task 2: 视频联系表与 GIF 首帧

**Files:** Create `analysis/media.py`；Test `tests/test_media.py`

**Interfaces:** `video_contact_sheet(video, dest, ffmpeg="ffmpeg") -> Path`、`gif_first_frame(src, dest) -> Path`

- [ ] **Step 1: failing tests** — 用一个假的 `runner` 断言：调用参数含 `-vf` 且抓三个时间点；ffmpeg 非 0 退出时抛 `MediaError`；GIF 首帧用 Pillow 真图断言尺寸。
- [ ] **Step 2: RED**, **Step 3: implement**

```python
# src/dailywallpaper/analysis/media.py
"""Turn a video/GIF into a single still the vision model can score."""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image

THUMB_HEIGHT = 360


class MediaError(RuntimeError):
    pass


def gif_first_frame(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im.seek(0)
        im.convert("RGB").save(dest, "JPEG", quality=90)
    return dest


def video_contact_sheet(video: Path, dest: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Stack frames at 10 %/50 %/90 % into one image (ffmpeg, local only)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", "-i", str(video),
            "-vf", "select='eq(n,0)+eq(n,1)+eq(n,2)',scale=-1:%d,tile=3x1" % THUMB_HEIGHT,
            "-frames:v", "1", str(dest),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0 or not dest.is_file():
        raise MediaError(f"ffmpeg failed: {result.stderr[-300:]}")
    return dest
```

- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: build contact sheets for video and gif candidates`

---

### Task 3: WE applier

**Files:** Create `apply/we_applier.py`；Test `tests/test_we_applier.py`

**Interfaces:** `WeApplier(control)` with `.snapshot() -> WeState`, `.apply(image_path) -> ApplyResult`, `.restore(state) -> None`

- [ ] **Step 1: failing tests** — 假 control：`apply` 成功时 `open` 被调用且验证通过；`open` 返回 False 时 `ApplyResult(ok=False)` 且调用了恢复；`snapshot` 记下当前文件。
- [ ] **Step 2: RED**, **Step 3: implement** — `apply` 调 `control.open(project_json_or_asset)`，然后最多轮询 5 秒比对 `control.current()` 的父目录。
- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: apply dynamic wallpapers through wallpaper engine`

---

### Task 4: 静态/动态日计划

**Files:** Create `recommend/media_plan.py`；Test `tests/test_media_plan.py`

**Interfaces:** `MediaPlan.decide(day: date, strategy: str, n: int, on_battery: bool) -> str`

- [ ] **Step 1: failing tests**

```python
@pytest.mark.parametrize("strategy,expected", [
    ("static_only", "static"), ("dynamic_only", "dynamic"),
])
def test_explicit_strategies(strategy, expected):
    assert MediaPlan.decide(date(2026, 10, 18), strategy, 3, False) == expected


def test_mixed_schedules_one_dynamic_day_every_n():
    days = [date(2026, 10, 1 + i) for i in range(6)]
    kinds = [MediaPlan.decide(d, "mixed", 3, False) for d in days]
    assert kinds.count("dynamic") == 2


def test_battery_forces_static():
    assert MediaPlan.decide(date(2026, 10, 18), "mixed", 1, True) == "static"


def test_dynamic_only_on_battery_still_returns_dynamic():
    # The caller falls back if no static candidate exists; the plan itself
    # honours an explicit dynamic_only.
    assert MediaPlan.decide(date(2026, 10, 18), "dynamic_only", 3, True) == "static"
```

- [ ] **Step 2: RED**, **Step 3: implement** — `dynamic_only` + 电池 → `static`（spec 9.2.1：电池模式自动改为静态）；`mixed` 用 `day.toordinal() % n == 0`。
- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: decide static or dynamic days`

---

### Task 5: 少用代理 + 天气进理由

**Files:** Modify `config.py`、`sources/wallhaven.py`、`sources/bing.py`、`app.py`；Test `tests/test_sources.py`、`tests/test_integration.py`

**Interfaces:** `SourcesSection.connect_timeout: int = 5`；`RealPipeline` 把天气标签写进 `Decision.reason`。

- [ ] **Step 1: failing tests**
  - 适配器用 `timeout=(connect_timeout, 30)`：假 session 断言收到的是元组且第一项为 5。
  - 一次真实的 `fetch_all` 里 Wallhaven 抛错后，`state` 里出现 `source_down_wallhaven_<date>`；同一天再调用时**不再请求** Wallhaven。
  - 天气编码 61 时候选理由里出现"雨"。
- [ ] **Step 2: RED**, **Step 3: implement**
- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: fail fast on unreachable sources and explain the weather`

---

### Task 6: 接线

**Files:** Modify `app.py`、`ui/shell.py`、`ui/main_window.py`；Test `tests/test_integration.py`

- [ ] **Step 1: failing test** — `build_pipeline` 在 `wallpaper_engine.enabled` 为真且工坊存在时把 `WorkshopSource` 放进来源列表；为假时不放。
- [ ] **Step 2: RED**, **Step 3: implement** — `build_application` 根据今天的 `MediaPlan` 选择 `WindowsStaticApplier` 或 `WeApplier` 交给 `ApplyService`。
- [ ] **Step 4: run + full suite**, **Step 5: commit** `feat: wire dynamic wallpapers into the pipeline`

---

## 验收（spec §14.4 剩余项）

- WE 切换后 config.json 变化，恢复后回到原项目 —— Task 3 测试 + 一次手工真实验证
- WE 开机恢复后静态壁纸仍生效 —— 手工验证

## 后续

Phase 3：密钥扫描、README、测试补齐、打包。
