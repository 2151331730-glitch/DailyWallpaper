# scripts/probe_sources.py
import json, requests
from dailywallpaper.sources.wallhaven import WallhavenSource
from dailywallpaper.sources.bing import BingSource
from dailywallpaper.recommend.weather import fetch_weather, weather_tags

def main():
    with requests.Session() as s:
        wh = WallhavenSource(s).fetch(24)
        bing = BingSource(s).fetch()
        weather = fetch_weather(s, 39.9075, 116.39723)
    print(json.dumps({
        "wallhaven": [
            {"id": c.source_id, "res": f"{c.width}x{c.height}", "url": c.file_url}
            for c in wh[:5]
        ],
        "wallhaven_count": len(wh),
        "bing": [
            {"id": c.source_id, "res": f"{c.width}x{c.height}", "url": c.file_url}
            for c in bing
        ],
        "weather": {
            "temperature": weather.temperature,
            "code": weather.code,
            "tags": sorted(weather_tags(weather.code)),
        },
    }, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
