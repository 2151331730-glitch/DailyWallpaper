# Phase 1B 验收清单（spec §14.4）

| 项 | 状态 | 证据 |
|---|---|---|
| Phase 0 五个探针 | ✅ | `docs/spikes/phase0-results.md` |
| 静态壁纸设置后注册表指向正确文件 | ✅ | Phase 0 Probe 1 |
| 原壁纸能恢复 | ✅ | Phase 0 Probe 1 |
| WE 当前壁纸能读取（config.json） | ✅ | Phase 0 Probe 2 |
| WE 切换后 config.json 变化，恢复后回到原项目 | ✅ | Phase 0 Probe 2 |
| 视觉 JSON 可解析，token/成本记录正确 | ✅ | 2026-10-18 真实 dry-run（¥0.0026） |
| 同一天重复运行不会换第二次 | ✅ | `test_seven_day_loop_writes_one_success_per_day` |
| 所有候选不达标时原壁纸不变 | ✅ | `test_low_quality_candidates_are_gated_out` |
| 预算到顶后不再付费调用 | ✅ | `test_review_stops_when_the_budget_is_gone`、`test_check_blocks_over_cap` |
| 同一图片第二次分析命中缓存 | ✅ | `test_second_call_is_served_from_cache` |
| WE 开机恢复后静态壁纸仍生效 | ✅ | 5s/30s 复查并在 WE 抢回时重设（spec §9.1）：`test_a_static_apply_rechecks_for_a_we_takeover_at_5s_and_30s`；真实登录场景需开机自启时观察 |
| 第二个实例被单实例锁拦住 | ✅ | `test_single_instance_rejects_a_second_owner`；第二次启动现在只打印中文提示并退出 1，不再抛 traceback：`test_a_second_instance_exits_cleanly_with_a_message` |
| 幻灯片/Spotlight 模式有明确提示 | ✅ | 应用前读 `BackgroundType`，2/3 时尝试切回图片，失败则拒绝并提示（spec §9.1）：`test_slideshow_background_is_switched_back_to_picture`、`test_spotlight_background_is_switched_back_to_picture`、`test_a_slideshow_is_refused_when_it_cannot_be_switched_back` |
| 7 天模拟使用注入时钟 | ✅ | `test_one_apply_per_day_for_a_week` |
| 日志中没有 key | ✅ | `grep` 于 `data/logs/app.log` 无匹配 |
| Git 仓库中没有 key/数据文件 | ✅ | `git grep` 无匹配；`data/`、`config.toml` 未跟踪 |
