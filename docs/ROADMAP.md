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

测试：305 个，全绿。远端：`github.com/2151331730-glitch/DailyWallpaper`。

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

## 遗留（低优先）

- 托盘在 explorer 重启后不自动恢复（需要 Qt 原生事件过滤，收益低）
- 设置页改动不完全热生效（重启后生效）
- 视频候选在没有 ffmpeg 时退回海报图评分

## 已知的取舍

- Wallhaven 需要代理；Bing 国内直连可用。没有代理时自动只剩 Bing。
- `git push` 受全局 `http.proxy=127.0.0.1:7890`（未监听）影响，需 `-c http.proxy=` 绕过或修正配置。
- 设置页写回 `config.toml` 不保留注释；不认识的顶层表会原样保留。
