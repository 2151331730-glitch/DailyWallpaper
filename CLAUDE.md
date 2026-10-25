# DailyWallpaper · 运行态说明

> 只记**代码里读不出来的运行事实**。架构、功能、目录职责看 `README.md`；审美规范看 `docs/CURATION.md`；路线看 `docs/ROADMAP.md`。
> Claude Code 在本目录启动时自动读取。

## 现在跑的是哪个版本（2026-10-24 核实）

托盘里实际运行的进程：

```
.venv\Scripts\pythonw.exe -m dailywallpaper --config C:\Users\win11\DailyWallpaper\config.toml
```

即**源码版**，editable 安装（`.venv\Lib\site-packages\__editable__.dailywallpaper-0.1.0.pth` → `src`），改 `src\` 下次启动生效。

⚠️ 仓库里同时还存在冻结版 `dist\` 和安装包 `installer.iss`；如果哪天托盘是 `DailyWallpaper.exe` 起来的，改 `src\` **完全无效**，必须重新 PyInstaller 打包。动代码前先确认跑的是哪个：

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='DailyWallpaper.exe'" |
  Where-Object { $_.CommandLine -like '*dailywallpaper*' } |
  Select-Object ProcessId, CreationDate, CommandLine
```

## 开始工作的第一步：先立基线

动手改任何代码之前，先确认"现在是好的"，否则改完出问题分不清是不是自己改坏的。

```powershell
git status --short                       # 有哪些未提交改动（别把别人的改动当成自己的）
.venv\Scripts\python -m pytest -q        # 必须全绿，约 50 秒
```

当前基线（2026-10-25 实测）：**626 passed in 48s**。当天开工前积压了 41 个文件、约 2.1k 行未提交改动，已提交为检查点；之后每做完一件事就提交一次，别让工作区长期不落盘。
2026-10-24 封版时清掉了 10 个已合并的旧阶段分支和 `phase0-1a` worktree，现在只剩 `main`。

代码基线之外还有运行基线：托盘在跑、点「换一张」有反应、`data\logs\` 有今天的记录。

## 重启（唯一正确顺序：先停后起）

`app.py` 里有 `SingleInstance`（Windows 命名互斥体）。**旧进程没退干净就起新的，新进程会直接退出**，而 pythonw 没有控制台，你连那句"另一个实例已在运行"都看不到——表现为"重启了但没生效"。

1. 托盘右键 → 退出（走 `UiShell._quit`，会先 `worker.stop()`）；或按 CommandLine 精确 `Stop-Process -Id <pid>`
2. 重新拉起：
   ```powershell
   cd C:\Users\win11\DailyWallpaper
   start "" ".venv\Scripts\pythonw.exe" -m dailywallpaper --config "C:\Users\win11\DailyWallpaper\config.toml"
   ```
3. **验证新代码在跑**（不要只看托盘图标）：
   ```powershell
   (Get-Item src\dailywallpaper\apply\service.py).LastWriteTime   # 文件改的时间
   (Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
     Where-Object { $_.CommandLine -like '*dailywallpaper*' }).CreationDate  # 必须更晚
   ```
   再加一条：`data\logs\` 里出现全新的启动批次（media plan / tick）。

别按名字杀：`Stop-Process -Name pythonw` 会误伤别的 Python 桌面程序。

## 入口与常用命令

```powershell
.venv\Scripts\python -m dailywallpaper              # 桌面界面
.venv\Scripts\python -m dailywallpaper --headless   # 无界面常驻（只有调度，没有 Qt）
.venv\Scripts\python -m dailywallpaper --once       # 跑一次
.venv\Scripts\python -m dailywallpaper --dry-run    # 演算不真换（隐含 --once）
.venv\Scripts\python -m dailywallpaper --curate 300 # 策展墙，人工打分建种子
```

## 测试与提交前

```powershell
.venv\Scripts\python -m pytest -q        # 626 个用例 / 约 9.5k 行测试 / 约 48 秒
.venv\Scripts\python scripts\check_secrets.py   # 提交前必跑
```

- `live` 标记的用例打真实 API，默认跳过；只有设 `DAILYWALLPAPER_LIVE=1` 才跑。**不要为了跑测试打开它**。

## 数据与配置在哪

| 东西 | 位置 |
|---|---|
| 数据库 | `data\app.db`（SQLite WAL，schema v4，7 张表：wallpapers / analyses / wallpaper_analyses / decisions / change_log / rejections / ratings / api_usage / state / user_intents） |
| 已下载壁纸 | `data\wallpapers\`、`data\steam_workshop\`（被禁的项目在 `_banned\`） |
| 日志 | `data\logs\` |
| 配置 | 项目根 `config.toml`；模板 `config.example.toml` |
| 智谱 key | Windows 凭据管理器 `DailyWallpaper` / `zhipu` |
| Steam key | 同上 / `steam_api_key` |

**key 一律从凭据管理器读，不要打印**；`scripts\set_key.py` 写入。

## 改代码时的硬约定

- **改 `analysis/prompts.py` 的提示词必须 bump `PROMPT_VERSION`**。它参与分析缓存键，不 bump 会复用旧分数，看起来"改了没效果"。
- **不要绕过 `ApplyService` 直接改 Wallpaper Engine 的 `config.json`**。守护线程 `keep_visible()` 每 5 秒按自己的判断纠正桌面，绕过它会被反复覆盖，而且撤销记录（`change_log`）会失真。
- **改 schema 要加 `SCHEMA_VERSION` 并写迁移分支**（`db.py:migrate()`），线上库已经是 v4，不能只改建表语句。
- 视觉调用一律走 `VisionClient`（缓存 + 预算 + 校验），不要直接调 `ZhipuVision` 绕过预算守卫。
- 测试不许触网、不许动真实 `data\app.db`（测试用 tmp 路径注入）；集成测试里电池状态要注入，不要读真实电源。

## 当前阶段（2026-10-25）

0. **一个池子一条分数线**（2026-10-25 用户要求，取代原"静态/动态自适应"）：所有候选
   （图片 / scene / video）一起打分排序，分数 ≥ `min_score`（默认 45）就够格上桌面。
   两池比最高分、动态日/静态日、连续天数配额、`state.media.recent_types` 全部删除；
   `media_strategy` / `selection_margin` / `dynamic_max_streak` 等配置项保留解析但无人读取。
   电池是唯一还看媒体类型的地方（没插电跳过动态）。日志：`media plan: N candidate(s) at or above X`。
1. **动态壁纸切换**：冷启动先分离启动引擎再等就绪（`WeApplier._boot_engine`），切换前改写
   WE 的 `selectedwallpapers`，动态↔动态之间用**黑场**过渡（桌面壁纸先换纯黑图、停当前壁纸，
   WE 真正画出新图后再等 2 秒恢复原图）。冷启动**不**发 `closeWallpaper`（会把引擎拉起来）。
   静态应用则是先设图再关 WE。这几条都有回归测试，改动前先看 `tests/test_apply.py`。
2. **离线评估**：✅ 已封版。`scripts/evaluate.py` 读 `data/app.db` 只读回放，输出 `docs/EVALUATION.md`。
   - 数据：361 条评分里 **360 张可评估**（`scripts/backfill_analyses.py` 补齐了 211 张；
     1 张模型连续返回不合约的调色板，排除）。成对比较 11392 个问题，NDCG 是 5 折 × 每折发牌的 10 个池。
   - 结论：排序确实好过随机（成对 0.672 / NDCG 0.783 vs 随机 0.525），
     但**纯个性化（0.581）跑不赢纯探索（0.643）**——真正在起作用的是探索分；
     糖水判别式是单点最好，优势在折间波动之内。详见 `docs/EVALUATION.md` 与 `docs/ROADMAP.md`。

**性能现状（2026-10-25 实测）**：换一张 1–3 秒；启动/重启后的池子恢复约 45–55 秒，主要成本是
网络抓取和预览图下载，不是评分（`--budget 2` 后评分只占十几秒）。抓取、评分、材料化三个阶段
都检查 `should_stop`，点击能打断它们。候选量是真正的瓶颈：一天约 22 个候选、过 45 线的 5–8 个，
根因是 `atleast=2560x1440 & ratios=16x9` 的检索条件太严。

明确不做：再加壁纸来源、再改界面。云端服务化是封版之后另起的新项目，不在这条线里。
