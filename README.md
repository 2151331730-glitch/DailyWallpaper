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
- **评分 1–10 分**（越高越喜欢；5.5 是中性中点，偏好权重按 `(分数 − 5.5) / 4.5` 映射到 −1…+1）
- **55% 探索**：创意 + 新颖 + 风格稀有度 + 与上一张的差异
- **45% 个性化**：你的评分偏好（按时间衰减）+ 来源偏好 + 天气契合
- **场景加成（最多 +5 分，只加不扣）**：根据当前天气和时段，给调色板亮度、情绪标签更贴合的图加一点分。
  它只在接近的候选之间决定先后，不改变 55/45 的主排序，也不会因为“现在下雨”就排除一张你评分很高的亮图
- 没有任何评分记录时，个性化固定 60 分（冷启动不瞎猜）

### 桌面适配（本地计算，免费）
- 每张候选先做一次本地测量：平均亮度、边缘密度、任务栏区域的亮度
- 太亮、太杂、底部太亮的图会降权（最多 −9 分），太暗的轻微降权
- 不看画质，只看"铺到桌面上图标还看不看得清"；完全不调用模型

### 人工策展与冷启动种子
- `python -m dailywallpaper --curate [N]` 打开缩略图墙，给 N 张候选（默认 300）打分
- 每张图下面一排 1–10，点数字即打分；分数写进 `ratings`，不会覆盖已有记录
- 「生成种子」把已评分的图抽样分析后导出 `assets/seed_profile.json`
- 种子里有一份 `avoid_tags`（默认含 anime / game-art / screenshot 等）：命中的
  游戏/动漫场景会扣分，但创意够高、不套路的作品仍然保留
- 还有一份 `boost_tags`（默认含 surreal / mysterious / ethereal / dreamlike 等）：
  命中的超现实、神秘风格每词 +6 分，最多 +18
- 新装或别人使用时，没有评分会自动加载种子，不从 60 分冷启动

### 「补充一句」会做什么

评分框里的「补充一句」会被发送给你配置的智谱模型做一次分类（每句一次调用），
判断它是「对这张图的评价」「长期偏好」还是「搜索意图」。分类结果只保存在本机
数据库，可以在「今日」页撤销。**只有这句话会发送，不上传图片或本地文件。**

- 识别为偏好：合并进加/减分标签，永久生效
- 识别为搜索：加入搜索队列，点「换一张」立即应用，否则下次自动换图时生效
- 没识别出来：评语照常保存，但不影响推荐

### 声音控制
- 托盘「壁纸声音」开关：取消勾选即静音所有动态壁纸（`-control mute`）
- **每次启动默认静音**，勾选只在本次运行内有效；下次开机恢复静音
- WE 重启后会忘记静音，桌面守护线程每分钟重新按一次

### 界面
托盘常驻，四个标签页：**今日**（预览/分数/理由）、**历史**（90 天/评分）、
**成本**（本月花费/缓存命中率/明细）、**设置**（时间/城市/预算/来源开关/开机自启）。

托盘的「评分…」打开评分框，框里点一个 1–10 的分数就保存并关闭；
今日页的「评分…」打开同一个框，历史页可给任意一天补评分或改评分（双击行也行）。
评分对象是**当前桌面上的那张壁纸**：会读取 Wallpaper Engine / Windows 的实际状态，
即使你在 Wallpaper Engine 里手动换过，也不会评错成"最后一次决策"的图。
「换一张」立刻换（正在换时连点只算一次）；
「暂停自动更换」支持 1 天 / 7 天 / 直到手动恢复。

「换一张」的候选是按需下载的：一次准备会保留全部候选，点到哪张才下载哪张全尺寸图。
队列用完时才补充（每天最多 6 次），补充时最多重算 5 个候选，其余直接用缓存分析，
所以通常几十秒内就能继续换，不会出现“点几次就卡住”。

### 省钱与安全
- **¥10/月硬上限**，80% 时告警；4xx 不计费
- 同一张图只付一次，之后走缓存
- API key 只存 Windows 凭据管理器；日志写入前自动脱敏
- 单实例锁，开第二个会被拦住

---

## 安装（Windows，推荐）

1. 下载 `DailyWallpaper-Setup-0.1.0.exe`（约 82 MB）
2. 双击安装，**不需要管理员权限**，默认装到 `%LOCALAPPDATA%\Programs\DailyWallpaper`
   - 安装时可选「将任务栏和搜索框改成深色」，适配深色壁纸（只改当前用户，可随时改回）
3. 首次启动会要求填写智谱 API key（免费注册：open.bigmodel.cn → API Keys）
4. 安装时会问一次「是否使用作者的默认审美筛选」，可以选不使用

**想用 Steam 的 Wallpaper Engine 壁纸**：安装前先在自己电脑上装好 Steam 和
Wallpaper Engine（付费软件，需自行购买），程序启动时会自动找到它。国内打开
Steam 商店/创意工坊需要的设置，见安装目录下的《Steam 壁纸设置》（`docs/STEAM_SETUP.md`）。

卸载：设置 → 应用 → 每日壁纸 → 卸载。卸载不会删除 `%LOCALAPPDATA%\DailyWallpaper` 里的配置和数据。

> 安装包没有代码签名，Windows 可能提示「未知发布者」，点「仍要运行」即可。

## 从源码运行

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

### 让 Steam 在线工坊参与推荐（可选）

除了本机已订阅的工坊项目，程序还能从 Steam 在线工坊发现新壁纸，并在后台用
SteamCMD 提前下载好，换壁纸时零等待。

一次性准备：

```powershell
# 1. Steam Web API key（免费）：登录 steamcommunity.com/dev/apikey 申请
.venv\Scripts\python scripts\set_steam_key.py

# 2. 下载 Valve 官方的 SteamCMD 到 data/tools/steamcmd
.venv\Scripts\python scripts\install_steamcmd.py

# 3. 用拥有 Wallpaper Engine 的 Steam 账号登录一次（密码和 Steam Guard 在
#    控制台里手动输入，程序不保存密码；SteamCMD 自己缓存登录票证）
.venv\Scripts\python scripts\steam_login.py
```

然后在 `config.toml` 填账号名：

```toml
[steam_workshop]
enabled = true
steam_account = "你的 Steam 用户名"   # 只填用户名，不是密码
```

工作方式：

- 在线候选和本地候选一样参与评分；**没下载完的候选永远不会被选中**，所以到点换壁纸不会被下载卡住。
- 后台预取线程把评分最高的前 `prefetch_top_n` 个下到 `data/steam_workshop/`，缓存超过 `max_cache_gb` 时按最久未用淘汰，正在用的不删。
- 下载失败、没登录、网络不可用都只影响在线候选，本地/静态候选照常工作。
- SteamCMD 不会在 Steam 客户端里留下订阅；下载的是你账号本来就有权访问的内容。

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
| 下次启动（`startup`）且当天还没换 | 若已有预生成的 `next_wallpaper`，立即应用（<1 秒），不等时间窗 |

**代理**：设置页可以直接填代理地址并「测试代理」；程序每次准备候选前都会重新探测，代理晚启动也能自动接上。
早期版本只在启动时探测一次，开机早于代理时会一整天只用直连。

**预生成**：每次换完壁纸、或后台预热完成后，程序会顺带选出并下载下一张，存进 `state.next_wallpaper`
（文件和决策记录一起）。所以「选图」那几分钟发生在你不着急的时候；开机只剩写入壁纸这一步。

预生成**没有过期时间**：中间隔一天、甚至隔一周没开机，下次启动仍会直接用它。
只有当机器从上次换图之后一直没开过（没跑过任何一次预热）时，才退回原来的完整流程。

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
| `docs/` | 设计文档、实施计划、探针与验收记录；`docs/CURATION.md` 策展目标（审美原则） |
| `data/` | 运行期数据：`app.db`、`logs/`、`cache/`（Git 忽略） |

---

## 已知限制

- **Wallhaven 在国内需要代理**；没开代理时自动只剩 Bing（一天一张）。连不上的来源当天不再重试。
  可以在 `[sources] proxy` 填本地代理（例如 `http://127.0.0.1:7892`），程序启动时会先探测端口，
  没监听就自动退回直连；SteamCMD 也用同一个代理。
- 只操作**主显示器**。
- 依赖本地 Wallpaper Engine **正在运行**；未运行时动态日回退静态，不会自动启动它。
- **桌面守护**每 5 秒检查一次：Wallpaper Engine 若把你的壁纸盖回去，程序会把它关掉并恢复自己那张。
  想临时手动用 WE，先在托盘点「暂停自动更换」，暂停期间守护同样让路。
- 评分 **1–2 分**的壁纸会永久剔除：不再进候选池，已准备好的队列里也会被跳过；
  如果是 Wallpaper Engine 项目，程序还会把 WE 配置里“上次壁纸”的指向改掉，
  避免开机时 WE 自己把它恢复出来。它们的风格会以强负权重参与偏好学习。
- 设置页写回 `config.toml` 时**不保留注释**（不认识的顶层表会原样保留）。
- 没有安装包，按上面的 venv 方式运行。

---

## 赞助

这个项目是开源的，MIT 许可证。如果它帮到了你，可以通过爱发电请作者喝杯咖啡：

https://afdian.com/a/guoxinha

（托盘右键也有「支持作者」入口。）

## 安全

- key 只存在 Windows 凭据管理器，不写入项目文件
- `config.toml`、`.env`、`data/` 均不提交 Git
- `scripts/check_secrets.py` 在提交前扫描整个仓库
- 日志写入前过滤 `sk-…`、`Bearer …`、`api_key=…` 等形态
