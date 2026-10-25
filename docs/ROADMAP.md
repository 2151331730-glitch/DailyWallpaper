# DailyWallpaper 路线图

决策日期：2026-10-18。依据 `docs/superpowers/specs/2026-10-17-dailywallpaper-design.md` §15。

## 已完成

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 0 | 五个可行性探针 | ✅ 全部通过（`docs/spikes/phase0-results.md`） |
| Phase 1A | 无界面核心：数据层、来源、图片、视觉、预算、偏好、打分、应用、状态机、CLI | ✅ |
| Phase 1B | PySide6：托盘、今日/历史/成本/设置、评分窗、暂停/撤销/手动 | ✅ |
| — | 付费复核（`glm-4.6v` 复核前 N 名） | ✅ 真实链路验证，单次 ¥0.0026 |
| 1B 收尾 | `assets/icon.png`、`启动.bat`、spec §14.4 清单、低风险 Minor | ✅ |
| Phase 2 | 动态壁纸 + 反馈闭环（见下） | ✅ |
| Phase 3 | 工程化 + Git/GitHub | ✅ |
| Phase 4 | 自适应静态/动态选择（同池比较 + 校准 + 配额） | ✅ |
| Phase 5 | 离线评估 + 糖水判别式（封版前的最后一步） | ✅ |

测试：638 个，全绿。远端：`github.com/2151331730-glitch/DailyWallpaper`。封版 tag `v1.0.0`。

## Phase 5 交付内容（2026-10-24 封版）

- `scripts/evaluate.py`：只读回放 `data/app.db`，两种指标——成对比较（5 折交叉验证，
  11392 个"哪张更好"的问题）与 NDCG@5（每折自己发牌的 10 个候选池）。
- `scripts/backfill_analyses.py`：把缺当前版本分析的历史评分补齐。361 张评分从
  147 张可评估做到 360 张（1 张模型连续返回不合约字段，排除）。
- `docs/SUGARWATER.md` + `data/sugar_model.json`：糖水判别式的测量、门槛与留出检验。
- 测试 626 → 638。

**结论（`docs/EVALUATION.md` 为准）**：

| 问题 | 答案 |
|---|---|
| 排序比随机好吗 | 好。成对准确率 0.672 vs 0.5，NDCG@5 0.783 vs 随机 0.525 |
| 个性化那 45% 有用吗 | **没有证明有用**。纯个性化 0.581（低于纯探索 0.643），NDCG 也最低 |
| 糖水判别式值得留吗 | 单点最好（0.672），但比纯混合只高 0.022，在折间波动（±0.042）之内 |
| 模型分的分辨率够吗 | 不够。41 组四维分数完全相同，最大一组 39 张；相近的图模型分不开 |

**这些结论改变了对系统的认识**：真正在工作的是"探索分"（创意 + 新颖 + 稀有度 + 与上一张的差异），
个性化画像在 361 条评分、11392 次比较里没有跑赢它。下一步若要再动排序，先动画像的权重，
不要继续加特征。但这属于封版之后的新项目，1.0.0 不改。

## Phase 4 交付内容

- 静态图和动态壁纸在同一个池子里打分、排序，分数达到 `min_score`（默认 55）就够格上桌面。
  原来的"两个池子比最高分 / 动态日静态日 / 连续天数配额"已移除（2026-10-25）
- 联系表 / GIF 首帧分数按测得偏差校准（creativity +5.8 / novelty +3.1 / formulaic −2.1），
  作者封面与静态图不校准；`scripts/evaluate_mixed.py` 读同一个常量
- 电池是唯一还看类型的地方：没插电时跳过动态候选
- 每次决策打一行 `media plan: N candidate(s) at or above X`

## Phase 2 交付内容

- WE 本地 Workshop 扫描（含 `contentrating` 过滤）
- preview / GIF 首帧 / 视频三帧联系表（ffmpeg）
- `wallpaper64.exe -control openWallpaper` 切换、config.json 验证、失败恢复
- 静态/动态日选择 `media_strategy=mixed`，每 `dynamic_every_n_days` 天 1 天动态
- **电池策略**：`dynamic_requires_ac=true`，只有插电才应用动态壁纸
- 天气修正进入推荐理由
- 历史/撤销完善
- 静态应用后 5s/30s 复查 WE 是否抢回桌面；幻灯片/Spotlight 预先切回图片模式

**不做**：文字评价转标签（每次评语要额外付费调用）。

## Phase 3 交付内容

- `scripts/check_secrets.py`（提交前扫描密钥，已验证仓库干净）
- README 重写（覆盖 Phase 1 + Phase 2 的全部能力与已知限制）
- 每个 `src` 模块都有对应测试；`.gitignore` 覆盖 `config.toml` / `data/` / `*.log`
- 不做安装包

## 评分入口（2026-10-19 补齐）

- 评分量表改为 **1–10**（越高越喜欢）；偏好权重按 `(分数 − 5.5) / 4.5` 映射到 −1…+1。
- DB v2 迁移：旧的 −1 / 0 / 1 三档评分变为 1 / 5 / 10。
- 托盘：只保留「评分…」→ 弹出评分框，框里点 1–10 中的一个分数即保存并关闭。
- 今日页：「评分…」打开同一个评分框；状态区显示当前分数（如 `8 / 10`）。
- 历史页：选中任意一条 → 评分 / 改评分，双击该行同样打开；评分列显示 `x / 10`。
- 评分窗显示缩略图、标题、当前评分；改评分时恢复已选快捷理由，不再重复追加到评语。
- 评分目标按**桌面实际壁纸**解析：WE 按项目文件夹匹配（项目会把 project.json 解析成 scene.pkg / 视频），静态按 `data/wallpapers/<id>.apply.jpg` 匹配；解析不到才回退到最近一次成功应用，再回退到最新决策。今日页也显示这张实际壁纸。

## 候选池与多样性（2026-10-19）

- 症状：用户感觉「来来回回就这几张」。实测当天为动态日，本地 WE 工坊只有 12 个动态候选，已用掉 9 个；Wallhaven / Steam 在线源因为 Pigeon 的本地代理没有监听而全部失败。
- `[sources] proxy` 新增：requests 与 SteamCMD 都走这个代理；启动时先探测端口，没监听就退回直连（避免连 Bing 也被死代理拖死）。
- 在线工坊：`per_page` 20 → 50，`prefetch_top_n` 2 → 6，`max_cache_gb` 3 → 4，可用动态候选从「最多 2 个在线」扩大到 6 个。
- 手动「换一张」：短列表用尽时允许强制重建（每天最多 2 次），强制重建会忽略 30 分钟的空池节流并重试被标记失败的来源。
- 已下载的 Steam 在线工坊项：即使 Steam API 暂时不可用，也会从数据库重建候选加入池子（`_cached_steam_candidates`），不再只依赖本地 WE 工坊。

## 运行环境（2026-10-19 实测）

- 代理 Pigeon：已设 Run 键开机自启，`autoRun=true`（启动后自动连接）；Wallhaven、Steam API、SteamCMD 均经 `127.0.0.1:7892` 连通。
- Steam 在线工坊：后台预取已实测下载 4 项到 `data/steam_workshop/`，SteamCMD 使用已缓存的账号登录票证。
- 自启：`HKCU\...\Run` 里的 `DailyWallpaper` 指向 venv 的 `pythonw.exe` + 绝对 `config.toml`。

## 2026-10-21 收尾（反馈闭环）

- **1–2 分永久剔除延伸到 Wallpaper Engine**：评分后若 WE 的 `selectedwallpapers` 还指着这张，就把
  “上次壁纸”改写成当前壁纸（`replace_selection`）。此前只关 WE，开机后它又从 config 恢复，用户看到
  “评了 1 分还老是跳出来”。
- **负面学习保留**：1–2 分在偏好画像里不跳过，而是 1.5 倍强负权重（`BANNED_PENALTY`）。
  跳过会让“糖水/动漫”这类风格在新图上继续得分；强负权重才是正确做法。
- **工坊预取跳过 1–2 分项目**：`steam_prefetch` 的 SQL 加 `not exists (ratings <= 2)`，不再重复下载
  数百 MB 的被 ban 项目。
- **托盘「壁纸声音」**：`wallpaper64.exe -control mute/unmute`，状态存 `state.sound_enabled`。
  每次启动默认静音；WE 会忘记静音，所以桌面守护每分钟重发一次。
- **手动换图体验**：
  - refill 最多重算 5 个候选（`_reanalyze_budget`），其余复用缓存分析；refill 跳过复核
  - 候选按需下载（`_materialize_one`）：一次准备出的候选全部保留，点到哪张才下载哪张
  - 结果：一次准备可连续换约 40 次，不再“点三次就卡住”；refill 从 5–7 分钟降到几十秒
- **评分文件清理**：1–2 分壁纸的原图、缓存副本、工坊项目目录删除（本次 650.6 MB）。

## 2026-10-21 策展模式（冷启动种子）

- `python -m dailywallpaper --curate [N]`：从 Wallhaven 拉 N 张候选（默认 300），
  缩略图墙每张下面一排 1–10，点数字直接打分，分数实时写入 `ratings`。
- 打分不覆盖任何已有记录：同一条只更新它自己的分数。
- 「生成种子」：对已评分的图按每档评分抽 12 张、4 路并发调用视觉模型（缩略图足够
  给出风格标签），然后汇总成 `assets/seed_profile.json`。串行分析 120 张要 15 分钟，
  并发后约 4 分钟。
- 冷启动：数据库里没有评分时自动加载种子（`recommend/profile.py` → `_profile`），
  不再固定 60 分；用户自己的评分一出现就接管。
- 种子的 source 权重导出前做 L2 归一化（原始累加值在一个人身上可以是 -77，
  换个用户就没有可比性）。
- 本次策展：262 条评分，种子偏好集中在 dark / introspective / abstract /
  ominous / mysterious，回避 urban / serene / fantasy / vibrant / sunset。
- `avoid_tags`：种子里列出不想多看的类别（默认 anime / manga / cartoon /
  game-art / game-scene / screenshot / fan-art）。`scoring.avoid_penalty`
  命中扣 12 分/条，但 creativity ≥ 60 且 formulaic ≤ 40 的作品豁免——游戏/动漫
  题材有真东西时不封杀。
- `boost_tags`：列出想多看的类别（surreal / dreamlike / dreamscape / ethereal /
  otherworldly / mystical / mysterious / esoteric / occult / uncanny / liminal /
  eerie / cosmic）。`scoring.boost_score` 命中 +6 分/条，封顶 +18，防止靠堆标签取胜。

## 2026-10-21 桌面适配（B 方案）

- 新增 `analysis/desktop_fit.py`：把候选缩到 320×180，计算平均亮度、亮度标准差、
  边缘密度、底部 12%（任务栏区域）的平均亮度，输出 0–100 的"当桌面适不适合"分。
- 太亮（>170）、太暗（<35）、任务栏区域太亮（>190）、边缘密度太高（>0.30）分别
  扣分；纯本地 PIL/numpy，免费，每张约几十毫秒。
- `scoring.final_score` 增加 `desktop_fit` 参数：以 60 为中心、0.15 权重做加减，
  即最多 −9 / +6 分。它是微调，不推翻画质和偏好。
- 视觉模型管"这张图好不好"，桌面适配管"铺到你的桌面上好不好用"，两件事分开。

## 遗留（低优先）

- 托盘在 explorer 重启后不自动恢复（需要 Qt 原生事件过滤，收益低）
- 设置页改动不完全热生效（重启后生效）
- 视频候选在没有 ffmpeg 时退回海报图评分

## 已知的取舍

- Wallhaven 需要代理；Bing 国内直连可用。没有代理时自动只剩 Bing。
- `git push` 受全局 `http.proxy=127.0.0.1:7890`（未监听）影响，需 `-c http.proxy=` 绕过或修正配置。
- 设置页写回 `config.toml` 不保留注释；不认识的顶层表会原样保留。
