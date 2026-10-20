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

测试：389 个，全绿。远端：`github.com/2151331730-glitch/DailyWallpaper`。

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

## 遗留（低优先）

- 托盘在 explorer 重启后不自动恢复（需要 Qt 原生事件过滤，收益低）
- 设置页改动不完全热生效（重启后生效）
- 视频候选在没有 ffmpeg 时退回海报图评分

## 已知的取舍

- Wallhaven 需要代理；Bing 国内直连可用。没有代理时自动只剩 Bing。
- `git push` 受全局 `http.proxy=127.0.0.1:7890`（未监听）影响，需 `-c http.proxy=` 绕过或修正配置。
- 设置页写回 `config.toml` 不保留注释；不认识的顶层表会原样保留。
