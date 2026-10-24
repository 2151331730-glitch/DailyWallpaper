# scripts/analyze_dynamic.py
"""Does a contact sheet score like a single picture?

The app scores a video from three tiled frames and a still from one image.
If the model reads the same wallpaper differently in those two shapes, then
mixing static and dynamic candidates in one ranking compares two rulers.

Runs the app's own vision client over both versions of every item in the
dynamic batch (cached, so a rerun is free) and reports the difference.

    .venv\\Scripts\\python scripts\\analyze_dynamic.py --analyze   # costs ~¥0.1
    .venv\\Scripts\\python scripts\\analyze_dynamic.py             # report only
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import keyring  # noqa: E402

from dailywallpaper.analysis.images import canonicalize  # noqa: E402
from dailywallpaper.analysis.vision import (  # noqa: E402
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    VisionClient,
    ZhipuVision,
)
from dailywallpaper.config import load_config  # noqa: E402
from dailywallpaper.cost.budget import BudgetGuard  # noqa: E402
from dailywallpaper.db import connect, migrate, now_iso  # noqa: E402
from dailywallpaper.state.machine import SystemClock  # noqa: E402

DIMENSIONS = ("quality", "creativity", "novelty", "formulaic")
BATCH = ROOT / "data" / "concept_batch_dynamic"
SOURCE = "concept_batch"


def _ensure(conn, key: str, path: Path) -> int:
    conn.execute(
        """insert into wallpapers(source,source_id,title,local_path,media_type,created_at)
           values(?,?,?,?,'image',?)
           on conflict(source,source_id) do update set local_path=excluded.local_path""",
        (SOURCE, key, "", str(path), now_iso()))
    conn.commit()
    return conn.execute(
        "select id from wallpapers where source=? and source_id=?",
        (SOURCE, key)).fetchone()[0]


def analyze(conn, cfg) -> int:
    api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    if not api_key:
        print("凭据管理器里没有智谱 key")
        return 1
    client = VisionClient(
        provider=ZhipuVision(api_key,
                             max_output_tokens=cfg.vision.max_output_tokens,
                             temperature=cfg.vision.temperature),
        conn=conn, pricing=cfg.vision.pricing,
        budget=BudgetGuard(conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio,
                           SystemClock()),
        max_output_tokens=cfg.vision.max_output_tokens)
    meta = json.loads((BATCH / "meta.json").read_text(encoding="utf-8"))
    done = 0
    for item in meta["items"]:
        for suffix, relative in (("", item["thumb"]), ("-single", item["single"])):
            path = BATCH / relative
            if not path.exists():
                continue
            key = item["key"] + suffix
            wallpaper_id = _ensure(conn, key, path)
            canonical, image_hash = canonicalize(path, BATCH / "normalized")
            try:
                client.analyze_or_cache(wallpaper_id, image_hash, canonical,
                                        cfg.vision.screen_model)
                done += 1
            except Exception as exc:
                print(f"  {key}: {type(exc).__name__}")
    print(f"分析完成 {done} 次调用")
    return 0


def _scores(conn, key: str):
    return conn.execute(
        """select a.quality,a.creativity,a.novelty,a.formulaic
             from wallpapers w
             join wallpaper_analyses wa on wa.wallpaper_id=w.id
             join analyses a on a.id=wa.analysis_id
            where w.source=? and w.source_id=?
            order by a.id desc limit 1""",
        (SOURCE, key)).fetchone()


def report(conn) -> str:
    meta = json.loads((BATCH / "meta.json").read_text(encoding="utf-8"))
    rows = []
    for item in meta["items"]:
        contact = _scores(conn, item["key"])
        single = _scores(conn, item["key"] + "-single")
        if contact is None or single is None:
            continue
        rows.append((item, contact, single))

    by_kind: dict[str, list] = {}
    for item, contact, single in rows:
        by_kind.setdefault(item["kind"], []).append((contact, single))
    sheeted = [row for row in rows if row[0]["kind"] in ("video", "gif")]

    lines = [
        "# 联系表 vs 单帧：分数可比吗", "",
        f"样本 {len(rows)} 个动态壁纸（视频 {len(by_kind.get('video', []))}，"
        f"GIF {len(by_kind.get('gif', []))}，仅封面 {len(by_kind.get('cover', []))}）。",
        "",
        "差值 = 联系表分数 − 单帧分数。负数表示模型看拼图时给得更低。",
        "",
        "| 维度 | 视频 | GIF | 仅封面 | 噪声底 |",
        "|---|---|---|---|---|",
    ]
    # Measured on 2026-10-24 by re-encoding 8 cover images at a different JPEG
    # quality (different bytes, same picture, no cache hit). Anything at or
    # below this is the model talking to itself, not a real difference.
    noise = {"quality": 1.9, "creativity": 0.0, "novelty": 0.6, "formulaic": 0.6}
    means: dict[str, float] = {}
    for index, name in enumerate(DIMENSIONS):
        cells = []
        for kind in ("video", "gif", "cover"):
            group = by_kind.get(kind, [])
            if not group:
                cells.append("—")
                continue
            value = statistics.fmean(contact[index] - single[index]
                                     for contact, single in group)
            cells.append(f"{value:+.1f}")
        means[name] = (
            statistics.fmean(contact[index] - single[index]
                             for _, contact, single in sheeted) if sheeted else 0.0
        )
        lines.append(f"| {name} | {cells[0]} | {cells[1]} | {cells[2]} "
                     f"| ±{noise[name]:.1f} |")

    worst = max((abs(means[name]) for name in ("creativity", "novelty")), default=0)
    lines += ["", "## 结论", ""]
    if worst > 4:
        lines += [
            "**不可直接混池。** 联系表在创意/新颖上被系统性低估，"
            "幅度远超噪声底。混排前要按维度补回偏移（见下表），"
            "否则动态壁纸在同池排序里会天然吃亏。",
            "",
            "| 维度 | 建议校准（加到联系表分数上） |",
            "|---|---|",
            *[f"| {name} | {-means[name]:+.1f} |" for name in DIMENSIONS
              if abs(means[name]) > noise[name]],
        ]
    else:
        lines += ["**可以直接混池。** 差异落在噪声范围内，两种形状同一把尺子。"]

    lines += [
        "",
        "## 注意",
        "",
        "- 视频 12 个、GIF 12 个，均值标准误约 ±3 分；校准值按方向用，别当精确补偿。",
        "- 仅封面一组差值为 0 是缓存命中（同样的字节 = 同样的哈希），"
        "它不能用来证明模型稳定，噪声底是另做的重编码测量。",
        "- 样本里 8 个 `scene.pkg` 只能看作者封面，它们测的是封面不是壁纸。",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "docs" / "DYNAMIC-COMPARABILITY.md")
    args = parser.parse_args(argv)

    conn = connect(ROOT / "data" / "app.db")
    migrate(conn)
    if args.analyze and analyze(conn, load_config(args.config)):
        return 1
    text = report(conn)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
