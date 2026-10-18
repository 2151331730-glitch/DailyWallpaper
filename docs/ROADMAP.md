# DailyWallpaper 路线图

决策日期：2026-10-18。依据 `docs/superpowers/specs/2026-10-17-dailywallpaper-design.md` §15。

## 已完成

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 0 | 五个可行性探针 | ✅ 全部通过（`docs/spikes/phase0-results.md`） |
| Phase 1A | 无界面核心：数据层、来源、图片、视觉、预算、偏好、打分、应用、状态机、CLI | ✅ |
| Phase 1B | PySide6：托盘、今日/历史/成本/设置、评分窗、暂停/撤销/手动 | ✅ 主体完成 |
| — | 付费复核（`glm-4.6v` 复核前 N 名） | ✅ 真实链路验证，单次 ¥0.0026 |

测试：243 个，全绿。远端：`github.com/2151331730-glitch/DailyWallpaper`。

## 剩余顺序（已确认）

### 1. 1B 收尾
- `assets/icon.png`、项目内 `启动.bat`
- 剩余低风险 Minor
- 逐条过 spec §14.4 验收清单

### 2. Phase 2（一份完整计划，不拆分）
- WE 本地 Workshop 扫描（含 `contentrating` 过滤）
- preview / GIF 首帧 / 视频三帧联系表（ffmpeg）
- `wallpaper64.exe -control openWallpaper` 切换、config.json 验证、失败恢复
- 静态/动态日选择 `media_strategy=mixed`，每 `dynamic_every_n_days` 天 1 天动态
- **电池策略**：`dynamic_requires_ac=true`，只有插电才应用动态壁纸
- 天气修正进入推荐理由
- 历史/撤销完善

**不做**：文字评价转标签（每次评语要额外付费调用）。

### 3. Phase 3 工程化
- 密钥扫描脚本、README 重写、测试补齐
- 可选：打包（不做安装包）

## 已知的取舍

- Wallhaven 需要代理；Bing 国内直连可用。没有代理时自动只剩 Bing。
- `git push` 受全局 `http.proxy=127.0.0.1:7890`（未监听）影响，需 `-c http.proxy=` 绕过或修正配置。
- 设置页写回 `config.toml` 不保留注释；不认识的顶层表会原样保留。
