# Phase 0 Results (2026-10-17)

- Worktree: `C:\Users\win11\DailyWallpaper\.claude\worktrees\phase0-1a`
- Branch: `worktree-phase0-1a`
- Spec: `docs/archive/superpowers/specs/2026-10-17-dailywallpaper-design.md`

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

Command:

```powershell
.venv\Scripts\python.exe scripts\probe_vision.py --image "D:\games\steam\steamapps\workshop\content\431960\1132505365\preview.jpg"
```

Result: **PASS** (2026-10-18).

Evidence:

```json
{
  "model": "glm-4.6v-flash",
  "scores": {
    "quality": 85,
    "creativity": 80,
    "novelty": 75,
    "formulaic": 20,
    "nsfw": false,
    "watermark": false,
    "text_overlay": "none",
    "style_tags": ["anime", "landscape", "minimal", "dark"],
    "mood_tags": ["calm", "cozy", "vibrant"],
    "palette": ["#ff8c00", "#ff6600", "#8b4513", "#4b0082", "#d3d3d3"],
    "subject": "woman on bamboo raft at sunset",
    "reason": "serene, artistic landscape with warm colors"
  },
  "tokens_in": 1845,
  "tokens_out": 152,
  "reasoning_tokens": 0,
  "latency_ms": 7234
}
```

Notes:

- `thinking: {"type": "disabled"}` is required. With the default thinking the reasoning pass consumed the whole 500-token budget (`finish_reason=length`, `reasoning_tokens=489`, content `'\n'`). Disabled, the same budget finishes with `finish_reason=stop` and `reasoning_tokens=0`.
- `max_tokens=500` was kept: 152 completion tokens, stopped normally, so the budget is sufficient once thinking is off.
- The provider returns `style_tags`, `mood_tags` and `palette` as comma-separated strings instead of the arrays the JSON schema asks for, and the palette carries no `#` prefix. `validate_scores` now accepts both forms and restores the `#`.
- HTTP 429 (provider code 1305, model overloaded) appears intermittently. The probe retries with 30/60/120/180 s backoff; retry policy on the product path is deferred to Task 1.4 (`VisionClient`).
- Key: stored in Windows Credential Manager (`DailyWallpaper` / `zhipu`); the value was never printed or logged.

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
| Vision | PASS |
| Sources and weather | PASS |
| Environment and keyring | PASS |

All five probes pass. Phase 0 is closed; Phase 1A may proceed.

## Deferred

- Blur-threshold calibration with 20 candidate thumbnails: deferred to Task 1.3, where the prefilter and sample images exist.
