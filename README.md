# 每日壁纸（DailyWallpaper）

Windows 本地 AI 自动壁纸应用。每天从 Wallhaven / Bing / 本地 Wallpaper Engine 工坊挑选一张壁纸，
先用视觉模型打分筛选，再按"探索/个性化"加权排序，选出当天的一张自动设为桌面壁纸。

## 当前状态

- Phase 0（可行性探针）：**5/5 PASS**
- Phase 1A（headless 核心）：**完成** —— 数据层、来源、图片处理、视觉分析、预算、偏好、打分、
  应用/撤销、状态机、CLI 全部就位，集成测试覆盖"连续 7 天每天恰好一次成功切换"。
- GUI（PySide6）属于后续计划。

设计文档：`docs/superpowers/specs/2026-10-17-dailywallpaper-design.md`
实施计划：`docs/superpowers/plans/2026-10-17-dailywallpaper-phase0-1a.md`
Phase 0 结果：`docs/spikes/phase0-results.md`

## 环境准备

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

把 API key 存进 Windows 凭据管理器（服务名 `DailyWallpaper`，账户名 `zhipu`）：

```powershell
.venv\Scripts\python scripts\set_key.py        # 终端输入
.venv\Scripts\python scripts\set_key_gui.py    # 图形对话框（无 TTY 时用）
```

复制配置模板并修改：

```powershell
copy config.example.toml config.toml
```

## 运行

```powershell
# 只演算，不真正换壁纸（仍会调用免费的视觉模型初筛并写数据库）
.venv\Scripts\python -m dailywallpaper --once --dry-run

# 执行当天的一次调度（到点且有候选就换壁纸）
.venv\Scripts\python -m dailywallpaper --once

# 常驻，每 60 秒 tick 一次
.venv\Scripts\python -m dailywallpaper
```

常用参数：`--config <路径>` 指定配置文件（默认 `./config.toml`）。

调度规则（`src/dailywallpaper/state/machine.py`）：

| 时刻 | 行为 |
|---|---|
| 早于 `app.daily_time`（默认 08:30） | 后台预取并分析候选 |
| `daily_time` ~ `latest_change_time`（默认 22:00） | 换壁纸 |
| 晚于 `latest_change_time` | 当天不再换 |
| 当天已成功换过 | 跳过 |
| `state.paused_until` 未到期 | 暂停 |

## 测试

```powershell
.venv\Scripts\python -m pytest -q
```

## 目录

| 路径 | 内容 |
|---|---|
| `src/dailywallpaper/` | 产品代码 |
| `src/dailywallpaper/sources/` | Wallhaven / Bing 适配器、去重入库 |
| `src/dailywallpaper/analysis/` | 图片规范化、预筛、视觉调用与缓存、打分排序 |
| `src/dailywallpaper/cost/` | 用量账本与月度预算守卫 |
| `src/dailywallpaper/recommend/` | 偏好画像、天气 |
| `src/dailywallpaper/apply/` | 静态壁纸 / Wallpaper Engine 控制、应用与撤销 |
| `src/dailywallpaper/state/` | 每日调度状态机 |
| `scripts/` | 探针与运维脚本（非产品代码） |
| `tests/` | 测试 |
| `data/` | 运行期数据：`app.db`、`logs/app.log`、`cache/`（Git 忽略） |

## 安全

- API key 只存在 Windows 凭据管理器，不写入项目文件。
- `config.toml`、`.env`、`data/` 均不提交 Git。
- 日志写入前会过滤 `sk-…`、`Bearer …`、`api_key=…` 等形态的密钥。

## 预算

`[budget] monthly_cny`（默认 ¥10/月）。每次付费调用前预写一条估算记录，调用后结算；
4xx 响应不计入花费。达到 80% 时记录一次告警（每月一次），达到上限停止调用。
