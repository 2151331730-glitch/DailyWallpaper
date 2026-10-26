# scripts/backfill_analyses.py
"""Give every rated wallpaper a current-version analysis.

The offline report can only judge a wallpaper it has scored. Of 361 ratings,
147 carry a v3 analysis; the rest were rated before the current rubric ran
(or after their cached file was cleaned up), so every conclusion in
`docs/EVALUATION.md` rests on less than half of the rating history.

This script closes that gap from what is available:

- a downloaded local file or a normalised cache copy is used as-is;
- a video is scored from a contact sheet, a GIF from its first frame -- the
  same shapes the app itself scores, so the numbers stay comparable;
- otherwise the remote preview is downloaded (Wallhaven's `lg` thumbnail is
  enough: the app scores previews, not originals).

    .venv\\Scripts\\python scripts\\backfill_analyses.py --dry-run   # what would run
    .venv\\Scripts\\python scripts\\backfill_analyses.py             # ~¥0.5

A rerun is nearly free: `VisionClient` hits the (image_hash, model, prompt)
cache for anything already scored.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import keyring  # noqa: E402
import requests  # noqa: E402

from dailywallpaper.analysis.images import canonicalize, download  # noqa: E402
from dailywallpaper.analysis.media import gif_first_frame, video_contact_sheet  # noqa: E402
from dailywallpaper.analysis.prompts import PROMPT_VERSION  # noqa: E402
from dailywallpaper.analysis.vision import (  # noqa: E402
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    VisionClient,
    provider_from_config,
)
from dailywallpaper.config import load_config  # noqa: E402
from dailywallpaper.constants import ESTIMATE_CNY, VIDEO_SUFFIXES  # noqa: E402
from dailywallpaper.cost.budget import BudgetGuard  # noqa: E402
from dailywallpaper.db import connect, migrate  # noqa: E402
from dailywallpaper.state.machine import SystemClock  # noqa: E402

# Ratings whose v3 analysis is missing: the report silently ignores these.
MISSING_SQL = """
    select r.wallpaper_id, r.rating, w.source, w.media_type,
           w.local_path, w.preview_url, w.file_url
      from ratings r
      join wallpapers w on w.id = r.wallpaper_id
     where not exists (
           select 1 from wallpaper_analyses wa
             join analyses a on a.id = wa.analysis_id
            where wa.wallpaper_id = r.wallpaper_id
              and a.prompt_version = ?)
     order by r.wallpaper_id
"""


def missing_rows(conn, prompt_version: str = PROMPT_VERSION):
    return conn.execute(MISSING_SQL, (prompt_version,)).fetchall()


def plan_source(row, video_suffixes=VIDEO_SUFFIXES):
    """Which copy of this wallpaper to score, without touching the network.

    Returns ``(kind, value)`` where kind is one of:

    - ``video``  -- a local video file (value = path), scored as a sheet;
    - ``gif``    -- a local GIF (value = path), scored from its first frame;
    - ``local``  -- any other local image (value = path);
    - ``remote`` -- a URL to download (value = url);
    - ``None``   -- nothing usable, value = why.

    Preference order matters: the contact sheet is what the app scored during
    a real run, so an item that still has its video file should not quietly
    fall back to a downloaded poster and land in the report as a different
    measurement.
    """
    local = row["local_path"]
    if local and Path(local).is_file():
        suffix = Path(local).suffix.lower()
        if suffix == ".gif":
            return "gif", Path(local)
        return "local", Path(local)

    asset = row["file_url"]
    if asset and Path(asset).is_file() and Path(asset).suffix.lower() in video_suffixes:
        return "video", Path(asset)

    preview = row["preview_url"]
    if preview:
        if Path(preview).is_file():
            return "local", Path(preview)
        if preview.lower().startswith(("http://", "https://")):
            return "remote", preview
    return None, "no local file and no preview url"


def _prepare(row, source_dir: Path, session, ffmpeg: str):
    """Turn the chosen source into the image the vision model should read."""
    kind, value = plan_source(row)
    wallpaper_id = row["wallpaper_id"]
    if kind is None:
        raise LookupError(value)
    sheet = source_dir / "sheets" / f"{wallpaper_id}.jpg"
    if kind == "video":
        return video_contact_sheet(value, sheet, ffmpeg=ffmpeg)
    if kind == "gif":
        return gif_first_frame(value, sheet)
    if kind == "local":
        return value
    return download(session, value, source_dir / "preview" / f"{wallpaper_id}.jpg")


def _client(conn, cfg):
    api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    if not api_key:
        raise SystemExit("凭据管理器里没有模型 key：先跑 scripts\\set_key_gui.py")
    return VisionClient(
        provider=provider_from_config(cfg, api_key),
        conn=conn,
        pricing=cfg.vision.pricing,
        budget=BudgetGuard(
            conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, SystemClock()
        ),
        max_output_tokens=cfg.vision.max_output_tokens,
    )


def backfill(conn, cfg, limit: int | None = None, dry_run: bool = False) -> dict:
    rows = missing_rows(conn)
    if limit is not None:
        rows = rows[:limit]
    counts = {"total": len(rows), "scored": 0, "cached": 0,
              "skipped": 0, "failed": 0, "cost": 0.0}
    if dry_run:
        kinds = {}
        for row in rows:
            kind, _ = plan_source(row)
            kinds[kind or "unusable"] = kinds.get(kind or "unusable", 0) + 1
        counts["kinds"] = kinds
        return counts

    client = _client(conn, cfg)
    session = requests.Session()
    if cfg.sources.proxy:
        session.proxies = {"http": cfg.sources.proxy, "https": cfg.sources.proxy}
    data_dir = Path(cfg.app.data_dir)
    source_dir = data_dir / "cache" / "backfill"

    for index, row in enumerate(rows, start=1):
        wallpaper_id = row["wallpaper_id"]
        if client.budget and not client.budget.check(ESTIMATE_CNY):
            print("月度预算用完了，停止（已完成的都留在库里）")
            break
        try:
            preview = _prepare(row, source_dir, session, cfg.tools.ffmpeg_path)
            canonical, image_hash = canonicalize(
                preview, data_dir / "cache" / "normalized"
            )
            before = client.budget.spent() if client.budget else 0.0
            record = client.analyze_or_cache(
                wallpaper_id, image_hash, canonical, cfg.vision.screen_model
            )
            after = client.budget.spent() if client.budget else 0.0
            counts["cost"] += after - before
            counts["cached" if record.cached else "scored"] += 1
        except LookupError as exc:
            counts["skipped"] += 1
            print(f"[{index}/{len(rows)}] {wallpaper_id}: 跳过（{exc}）")
            continue
        except Exception as exc:  # one bad file must not end the batch
            counts["failed"] += 1
            print(f"[{index}/{len(rows)}] {wallpaper_id}: {type(exc).__name__}: {exc}")
            continue
        if index % 10 == 0 or index == len(rows):
            print(f"[{index}/{len(rows)}] 已评分 {counts['scored']} "
                  f"缓存命中 {counts['cached']} 失败 {counts['failed']} "
                  f"花费 ¥{counts['cost']:.3f}")
    return counts


def coverage(conn) -> tuple[int, int]:
    """(rated wallpapers, rated wallpapers carrying a current analysis)"""
    total = conn.execute(
        "select count(distinct wallpaper_id) from ratings"
    ).fetchone()[0]
    return total, total - len(missing_rows(conn))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--limit", type=int, default=None,
                        help="只补前 N 张（试跑用）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计每张图会从哪儿取，不下载也不调用")
    args = parser.parse_args(argv)

    conn = connect(ROOT / "data" / "app.db")
    migrate(conn)
    total, covered = coverage(conn)
    print(f"评分 {total} 张，已有当前分析 {covered} 张，缺 {total - covered} 张")

    counts = backfill(conn, load_config(args.config),
                      limit=args.limit, dry_run=args.dry_run)
    if args.dry_run:
        print(f"计划处理 {counts['total']} 张：{counts['kinds']}")
        return 0
    print(f"完成：评分 {counts['scored']}，缓存命中 {counts['cached']}，"
          f"跳过 {counts['skipped']}，失败 {counts['failed']}，"
          f"花费 ¥{counts['cost']:.3f}")
    total, covered = coverage(conn)
    print(f"现在：评分 {total} 张，有当前分析 {covered} 张，缺 {total - covered} 张")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
