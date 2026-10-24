# scripts/build_dynamic_batch.py
"""Build the dynamic-wallpaper batch: contact sheet plus one frame each.

The app judges a video by a contact sheet -- three frames tiled side by side
(analysis/media.py). The question this batch exists to answer is whether that
sheet scores like a single picture at all, so every item is prepared twice:
the sheet the model sees in production, and one representative frame.

A scene.pkg cannot be rendered outside Wallpaper Engine, so those fall back to
the project's own preview and the item is marked ``cover``: its labels and
scores describe the cover, not the wallpaper.

    .venv\\Scripts\\python scripts/build_dynamic_batch.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dailywallpaper.analysis.media import video_contact_sheet  # noqa: E402

VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv"}
SHEET_HEIGHT = 360
THUMB = 720


def _ffmpeg() -> str:
    bundled = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
    return str(bundled) if bundled.exists() else "ffmpeg"


def _frame(video: Path, dest: Path, at: int = 2) -> bool:
    result = subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-ss", str(at), "-i", str(video),
         "-frames:v", "1", str(dest)],
        capture_output=True, timeout=120)
    return result.returncode == 0 and dest.exists()


def _gif_sheet(src: Path, dest: Path) -> bool:
    """Three frames from a looping GIF, tiled the way the app tiles a video."""
    try:
        with Image.open(src) as gif:
            total = getattr(gif, "n_frames", 1)
            picks = sorted({0, total // 3, (2 * total) // 3})
            frames = []
            for index in picks:
                gif.seek(index)
                frame = gif.convert("RGB")
                width = int(frame.width * SHEET_HEIGHT / frame.height)
                frames.append(frame.resize((width, SHEET_HEIGHT)))
            if not frames:
                return False
            sheet = Image.new("RGB", (sum(f.width for f in frames), SHEET_HEIGHT))
            left = 0
            for frame in frames:
                sheet.paste(frame, (left, 0))
                left += frame.width
            sheet.save(dest, "JPEG", quality=88)
            return True
    except Exception:
        return False


def _shrink(src: Path, dest: Path) -> bool:
    try:
        with Image.open(src) as image:
            image = image.convert("RGB")
            image.thumbnail((THUMB, THUMB))
            image.save(dest, "JPEG", quality=88)
        return True
    except Exception:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "concept_batch_dynamic")
    parser.add_argument("--workshop", type=Path, default=ROOT / "data" / "steam_workshop")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)

    thumbs = args.out / "thumbs"
    singles = args.out / "single"
    thumbs.mkdir(parents=True, exist_ok=True)
    singles.mkdir(parents=True, exist_ok=True)

    items, skipped = [], 0
    for project in sorted(p for p in args.workshop.iterdir() if p.is_dir()):
        if len(items) >= args.limit:
            break
        if project.name.startswith("_"):
            continue
        key = f"we-{project.name}"
        contact, single = thumbs / f"{key}.jpg", singles / f"{key}.jpg"
        kind = None

        videos = sorted(p for p in project.iterdir()
                        if p.suffix.lower() in VIDEO_SUFFIXES)
        if videos:
            try:
                video_contact_sheet(videos[0], contact, _ffmpeg())
            except Exception:
                pass
            if contact.exists() and _frame(videos[0], single):
                kind = "video"

        if kind is None:
            gif = project / "preview.gif"
            if gif.exists() and _gif_sheet(gif, contact) and _shrink(gif, single):
                kind = "gif"

        if kind is None:
            for name in ("preview.jpg", "preview.png"):
                source = project / name
                if source.exists() and _shrink(source, contact) and _shrink(source, single):
                    kind = "cover"
                    break

        if kind is None:
            skipped += 1
            continue

        title = project.name
        try:
            meta = json.loads((project / "project.json").read_text(encoding="utf-8"))
            title = meta.get("title") or title
        except Exception:
            pass
        items.append({
            "key": key, "source": "we", "source_id": project.name, "title": title,
            "origin": "dynamic", "kind": kind,
            "thumb": f"thumbs/{key}.jpg", "single": f"single/{key}.jpg",
        })

    (args.out / "meta.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    kinds = {}
    for item in items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    print(f"{len(items)} 个动态壁纸（{kinds}），跳过 {skipped} 个 -> {args.out / 'meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
