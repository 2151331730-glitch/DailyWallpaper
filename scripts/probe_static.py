import json, sys, time
from pathlib import Path
from PIL import Image
from dailywallpaper.apply.windows_static import WindowsStaticApplier

def main() -> int:
    if "--confirm" not in sys.argv:
        print("This changes your wallpaper briefly. Re-run with --confirm.")
        return 2
    test = Path("data/spikes/static-test.jpg")
    test.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2560, 1440), (30, 60, 90)).save(test)
    applier = WindowsStaticApplier()
    state = applier.snapshot()
    print("before:", json.dumps(state.__dict__, ensure_ascii=False))
    try:
        result = applier.apply(test)
        time.sleep(1)
        print("apply:", result)
        print("current:", applier.spi.get_wallpaper())
    finally:
        applier.restore(state)
        print("restored:", applier.spi.get_wallpaper())
    return 0 if result.ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
