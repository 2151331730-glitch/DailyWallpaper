# 每日壁纸（DailyWallpaper）

Windows 本地 AI 自动壁纸应用。每天从 **Bing / Wallhaven / 本地 Wallpaper Engine 工坊** 中挑选一张壁纸，
先用视觉模型打分筛选，再按"探索 / 个性化"加权排序，把当天最合适的那一张设为桌面。

支持**静态图片**和**Wallpaper Engine 动态壁纸**（scene / 视频），并可按策略安排"哪几天用动态"。

---

## 现在能做什么

### 自动换壁纸
- 每天 **08:30** 自动换（可配置），**22:00** 之后当天不再换
- **一天最多成功一次**，重复运行不会换第二次
- 候选全都不达标时**保留原壁纸**，不硬凑
- **动态壁纸日**：`media_strategy=mixed` 时每 N 天安排 1 天动态（默认 n=3）
- **电池保护**：默认只有插电时才应用动态壁纸

### 三级筛选
| 层 | 做什么 | 成本 |
|---|---|---|
| 本地预筛 | 分辨率、宽高比、模糊、黑边、重复图 | 免费 |
| 免费初筛 | `glm-4.6v-flash` 打画质/创意/新颖/套路四个分 | ¥0 |
| 付费复核 | `glm-4.6v` 复核排名前 N 的候选并重排 | ≈¥0.003/张 |

硬门槛：画质 <60、套路 >70、NSFW、水印、大段文字 → 直接淘汰。

### 打分
- **55% 探索**：创意 + 新颖 + 风格稀有度 + 与上一张的差异
- **45% 个性化**：你的评分偏好（按时间衰减）+ 来源偏好 + 天气契合
- 没有任何评分记录时，个性化固定 60 分（冷启动不瞎猜）

### 界面
托盘常驻，四个标签页：**今日**（预览/分数/理由）、**历史**（90 天/评分）、
**成本**（本月花费/缓存命中率/明细）、**设置**（时间/城市/预算/来源开关/开机自启）。

托盘的「喜欢 / 不喜欢」一键评分；「换一张」立刻换（正在换时连点只算一次）；
「暂停自动更换」支持 1 天 / 7 天 / 直到手动恢复。

### 省钱与安全
- **¥10/月硬上限**，80% 时告警；4xx 不计费
- 同一张图只付一次，之后走缓存
- API key 只存 Windows 凭据管理器；日志写入前自动脱敏
- 单实例锁，开第二个会被拦住

---

## 环境准备

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\python -m pip install -e ".[dev]"
```

把 API key 存进 Windows 凭据管理器（服务名 `DailyWallpaper`，账户名 `zhipu`）：

```powershell
.venv\Scripts\python scripts\set_key.py        # 终端输入
.venv\Scripts\python scripts\set_key_gui.py    # 图形对话框（无终端时用）
```

复制配置模板：

```powershell
copy config.example.toml config.toml
```

### 想让动态壁纸参与，填这两项

```toml
[wallpaper_engine]
install_dir = "D:\\games\\steam\\steamapps\\common\\wallpaper_engine"
```

工坊目录会自动从 Steam 库推导（`steamapps/workshop/content/431960`）。

---

## 运行

```powershell
.venv\Scripts\python -m dailywallpaper              # 桌面界面（推荐）
.venv\Scripts\python -m dailywallpaper --headless   # 无界面常驻
.venv\Scripts\python -m dailywallpaper --once       # 跑一次
.venv\Scripts\python -m dailywallpaper --dry-run    # 演算，不真换
```

也可以双击 **`启动.bat`**（用 `pythonw`，无控制台窗口）。

### 调度规则

| 时刻 | 行为 |
|---|---|
| 早于 `daily_time` | 后台预取并分析候选 |
| `daily_time` ~ `latest_change_time` | 换壁纸 |
| 晚于 `latest_change_time` | 当天不再换 |
| 当天已成功换过 | 跳过 |
| `state.paused_until` 未到期 | 暂停（手动换仍可用） |

---

## 测试

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\check_secrets.py    # 提交前跑一次
```

---

## 目录

| 路径 | 内容 |
|---|---|
| `src/dailywallpaper/sources/` | Bing / Wallhaven / 本地工坊适配器、去重入库 |
| `src/dailywallpaper/analysis/` | 图片规范化、预筛、视频联系表、视觉调用与缓存、打分排序 |
| `src/dailywallpaper/cost/` | 用量账本、月度预算、成本报表 |
| `src/dailywallpaper/recommend/` | 偏好画像、评分、天气、静态/动态日计划 |
| `src/dailywallpaper/apply/` | 静态壁纸 / Wallpaper Engine 控制、应用与撤销 |
| `src/dailywallpaper/state/` | 每日调度状态机 |
| `src/dailywallpaper/ui/` | PySide6 托盘、四个页面、评分窗 |
| `scripts/` | 探针与运维脚本（非产品代码） |
| `docs/` | 设计文档、实施计划、探针与验收记录 |
| `data/` | 运行期数据：`app.db`、`logs/`、`cache/`（Git 忽略） |

---

## 已知限制

- **Wallhaven 在国内需要代理**；没开代理时自动只剩 Bing（一天一张）。连不上的来源当天不再重试。
- 只操作**主显示器**。
- 依赖本地 Wallpaper Engine **正在运行**；未运行时动态日回退静态，不会自动启动它。
- 设置页写回 `config.toml` 时**不保留注释**（不认识的顶层表会原样保留）。
- 没有安装包，按上面的 venv 方式运行。

---

## 安全

- key 只存在 Windows 凭据管理器，不写入项目文件
- `config.toml`、`.env`、`data/` 均不提交 Git
- `scripts/check_secrets.py` 在提交前扫描整个仓库
- 日志写入前过滤 `sk-…`、`Bearer …`、`api_key=…` 等形态
