# 归档：过程文档

这里放的是项目**开发过程中的设计稿和实施计划**，不是当前系统的说明。

**读之前请注意**：这些文档描述的是当时的设计，其中一部分**已经被推翻了**。
它们保留下来是因为能看出「为什么当初这么做」，以及「后来为什么改了」——
但别把它们当作现在行为的依据。

## 内容

| 路径 | 是什么 | 现在的状态 |
|---|---|---|
| `superpowers/specs/2026-10-17-dailywallpaper-design.md` | 2026-10-17 的完整设计稿（第一版蓝图） | 大体骨架仍是现在的结构；**推荐理念「55% 探索 + 45% 个性化」已改为 25/75**，见 `docs/EVALUATION.md` |
| `superpowers/plans/2026-10-17-dailywallpaper-phase0-1a.md` | Phase 0-1A：探针 + 无界面核心 | 已完成 |
| `superpowers/plans/2026-10-18-*.md` | Phase 1B：付费复核、成本设置、评分历史、UI 外壳 | 已完成 |
| `superpowers/plans/2026-10-18-dailywallpaper-phase2-dynamic.md` | Phase 2：动态壁纸 + 反馈闭环 | 已完成；**其中的「每 N 天安排 1 天动态」已废弃**，现在是一个池子一条分数线 |

## 想知道现在的系统是什么样

按这个顺序读，比翻归档快得多：

1. `README.md` —— 能做什么、怎么装、评估结论
2. `docs/EVALUATION.md` —— 排序到底好不好的实测数据
3. `docs/CURATION.md` —— 审美原则和目标
4. `docs/ROADMAP.md` —— 分阶段做了什么、每一轮为什么改
5. 源码 `src/dailywallpaper/` —— 包结构就是架构（`sources/` `analysis/` `pipeline/` `apply/` `ui/`）

## 为什么要归档

这些文档加起来约 25 万字符，是实施计划而不是说明。留在 `docs/` 顶层时，
想读懂架构的人要先穿过七份「第 137 步：写测试」——对读者是噪音，
对作者也没有回头查的价值（Git 历史里都在）。
