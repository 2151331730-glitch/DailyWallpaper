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

当前基线（2026-10-24 实测）：**624 passed in 45s**。当天开工前积压了 41 个文件、约 2.1k 行未提交改动，已提交为检查点；之后每做完一件事就提交一次，别让工作区长期不落盘。
另有一个 worktree `.claude/worktrees/phase0-1a`（分支 `worktree-phase0-1a`，旧的 Phase 0 工作，别和 main 混用）。

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
.venv\Scripts\python -m pytest -q        # 624 个用例 / 约 9.6k 行测试 / 约 45 秒
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

## 当前阶段（2026-10-24）

0. **静态/动态自适应**：已完成（`docs/PLAN-media-selection.md` 阶段 4）。`mixed` 不再按日期轮换，
   两个池子都打分后比较（动态严格更高才换），联系表分数校准 +5.8/+3.1/−2.1，
   连续 2 天 / 10 天 5 天的动态配额。决策日志：`media decision: dynamic X vs static Y -> kind`。
   下一步是重启托盘观察一周（阶段 5）。
1. **离线评估框架**：第一版已完成。`scripts/evaluate.py` 读 `data/app.db` 只读回放，输出 `docs/EVALUATION.md`；11 个单测在 `tests/test_evaluate.py`。
   - 已知短板：可评估样本只有 147 张（361 条评分里其余没有 v3 分析），测试集 45 张只够切出 **1 个候选池**，NDCG 没有方差，结论只能当方向。
   - 下一步：补齐剩余 214 张的分析（约 ¥0.2），把池子做到 5–6 个再做结论。
2. **云端服务化**（2–3 周，可选）：把抓图 + 视觉打分搬到服务端定时跑，客户端只拉候选包。

明确不做：再加壁纸来源、再改界面。做完上面两件事就封版，只修 bug。
