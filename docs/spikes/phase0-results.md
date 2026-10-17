# Phase 0 Results (2026-10-17)

- Worktree: `C:\Users\win11\DailyWallpaper\.claude\worktrees\phase0-1a`
- Branch: `worktree-phase0-1a`
- Spec: `docs/superpowers/specs/2026-10-17-dailywallpaper-design.md`

## Probe 1: Static apply

Command:

```powershell
# WE was closed first, then:
.venv\Scripts\python.exe scripts\probe_static.py --confirm
# WE was reopened in finally
```

Result: **PASS**.

Evidence:

- Before: `WallPaper=C:\Users\win11\AppData\Roaming\Microsoft\Windows\Themes\WallpaperEngineOverride_randomXNLFPH.jpg`, `WallpaperStyle=22`, `TileWallpaper=0`.
- Apply: `ApplyResult(ok=True, error='')`.
- Current after apply: `data\spikes\static-test.apply.jpg` (verified via `SPI_GETDESKWALLPAPER`).
- Restored: `C:\Users\win11\AppData\Roaming\Microsoft\Windows\Themes\WallpaperEngineOverride_randomXNLFPH.jpg`.
- WE reopened: `2995369775/scene.pkg`.

Notes: WE had to be closed first; after `closeWallpaper`, WE's `config.json` still reports the selected project, but the desktop override is released. The static applier normalizes relative/absolute paths before verification.

## Probe 2: WE switch

Command:

```powershell
.venv\Scripts\python.exe scripts\probe_we.py --confirm
```

Result: **PASS**.

Evidence:

```json
{
  "original": "D:/games/steam/steamapps/workshop/content/431960/2995369775/scene.pkg",
  "results": {
    "scene": {"ok": true, "file": ".../1132505365/project.json", "current": ".../1132505365/scene.pkg"},
    "video": {"ok": true, "file": ".../1953370306/project.json", "current": ".../1953370306/mylivewallpapers.com-Cityscape-Cyberpunk.mp4"}
  },
  "restored": "D:/games/steam/steamapps/workshop/content/431960/2995369775/scene.pkg"
}
```

Notes: the real `config.json` stores the data at `win11.general.wallpaperconfig`, not at the top level. The parser now searches recursively. WE accepts `project.json` for both scene and video and resolves it to `scene.pkg`/mp4.

## Probe 3: Vision

Status: **PENDING** — waiting for a Zhipu API key.

- Code/tests: `tests/test_vision.py` passes (13 tests); the provider is exercised with an in-process fake client only.
- No real API call has been made.
- Once the key is stored, run:

```powershell
.venv\Scripts\python.exe scripts\probe_vision.py --image "D:\games\steam\steamapps\workshop\content\431960\1132505365\preview.jpg"
```

Expected: valid JSON scores, non-zero token usage, no key in output.

## Probe 4: Sources and weather

Command:

```powershell
.venv\Scripts\python.exe scripts\probe_sources.py
```

Result: **PASS**.

Evidence:

- Wallhaven: 24 candidates; first `jel1jq`, 3840×2160.
- Bing: 1 UHD candidate.
- Beijing weather: 15.0 °C, WMO code 3 -> `calm`, `muted`, `soft`.
- No images or thumbnails downloaded.

## Probe 5: Environment and keyring

Result: **PASS**.

Evidence:

- `keyring=25.7.0`, `PySide6=6.12.0`, `numpy=2.5.4`, `Pillow=13.0.0`, `requests=2.34.2`, `openai=3.33.0`.
- Keyring write/read/delete of a temporary `__probe__` value: ok.
- PySide6 offscreen window created: ok.
- `ffmpeg`/`ffprobe` 8.1.2.

## Go/No-Go

| Probe | Result |
|---|---|
| Static apply | PASS |
| WE switch | PASS |
| Sources and weather | PASS |
| Environment and keyring | PASS |
| Vision | PENDING key |

Phase 1A code may proceed, but Phase 0 is not fully closed until the real vision probe runs.

## Deferred

- Blur-threshold calibration with 20 candidate thumbnails: deferred to Task 1.3, where the prefilter and sample images exist.
