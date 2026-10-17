import json, sys, time
from pathlib import Path
from dailywallpaper.apply.we_control import WeConfig, WeControl

WE = Path("D:/games/steam/steamapps/common/wallpaper_engine")
CONFIG = WE / "config.json"
CONTENT = Path("D:/games/steam/steamapps/workshop/content/431960")

def pick(kind: str):
    for folder in CONTENT.iterdir():
        pj = folder / "project.json"
        if not pj.exists():
            continue
        data = json.loads(pj.read_text(encoding="utf-8"))
        if data.get("type") != kind:
            continue
        for name in ("project.json", "scene.pkg"):
            candidate = folder / name
            if candidate.exists():
                return candidate
        mp4 = next(folder.glob("*.mp4"), None)
        if mp4:
            return mp4
    return None

def main():
    if "--confirm" not in sys.argv:
        print("This briefly changes your wallpaper. Re-run with --confirm.")
        return 2
    cfg = WeConfig.from_file(CONFIG)
    original = cfg.current_file()
    ctl = WeControl(WE, CONFIG)
    results = {}
    try:
        for kind in ("scene", "video"):
            target = pick(kind)
            if not target:
                results[kind] = {"ok": False, "error": "no project"}
                continue
            ok = ctl.open(target)
            time.sleep(2)
            current = ctl.current()
            results[kind] = {
                "ok": ok and current.endswith(target.name),
                "file": str(target),
                "current": current,
            }
    finally:
        if original:
            ctl.open(Path(original))
            time.sleep(1)
        print(json.dumps({
            "original": original,
            "results": results,
            "restored": ctl.current(),
        }, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
