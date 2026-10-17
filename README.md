# 每日壁纸（DailyWallpaper）

Windows 本地 AI 自动壁纸应用。当前处于**设计评审阶段**，尚未开始产品代码。

## 当前状态

- 设计文档：`docs/superpowers/specs/2026-10-17-dailywallpaper-design.md`
- 已完成环境检查、来源/视觉 API 调研、Phase 0 探针计划
- 下一步：用户评审设计文档 → 写实施计划 → 执行 Phase 0 真实探针

## 技术栈

- Python 3.13 + PySide6
- SQLite
- GLM-4.6V-Flash（免费初筛）/ GLM-4.6V（付费复核）
- Wallhaven + Bing + 本地 Wallpaper Engine Workshop

## 目录

- `docs/`：设计文档与探针记录
- `src/`：产品代码（Phase 0 之后创建）
- `scripts/`：探针与运维脚本
- `tests/`：测试
- `data/`：运行期数据（Git 忽略）

## 安全

- API key 存 Windows 凭据管理器，不写入项目文件
- `config.toml`、`.env`、`data/` 均不提交 Git
- 提交前运行密钥扫描脚本（Phase 3 实现）

## 运行

Phase 0 之后补充。第一版不提供安装包，使用项目内 venv 运行。
