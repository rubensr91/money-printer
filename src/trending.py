"""
Trending Video Finder - MoneyPrinterV2
Finds trending/popular videos from TikTok (ES) and YouTube (ES).
No API keys needed. Uses TikTokApi (Playwright) + yt-dlp search.
"""

import os
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def get_youtube_trending_es(max_results=20):
    """
    Get popular YouTube videos in Spain using yt-dlp search.
    Searches for trending Spanish topics and returns recent popular videos.
    Falls back to popular Spanish search terms.
    """
    results = []

    search_terms = [
        "ESPAÑA hoy",
        "noticias España",
        "fútbol español",
        "viral España",
    ]

    for term in search_terms:
        if len(results) >= max_results:
            break
        try:
            cmd = [
                sys.executable, "-m", "yt_dlp",
                f"ytsearch{max_results}:{term}",
                "--flat-playlist",
                "--dump-json",
                "--no-playlist",
                "--match-filter", "view_count >= 50000",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            for line in proc.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    v = json.loads(line)
                    vid = v.get("id", "") or v.get("url", "").split("=")[-1] if v.get("url") else ""
                    if not vid or any(r["id"] == vid for r in results):
                        continue
                    title = v.get("title", "")
                    if not title:
                        continue
                    duration = v.get("duration", 0) or 0
                    views = v.get("view_count", 0) or 0
                    channel = v.get("channel", "") or v.get("uploader", "")
                    url = v.get("webpage_url", "") or v.get("url", "") or f"https://youtube.com/watch?v={vid}"

                    results.append({
                        "id": vid,
                        "title": title,
                        "url": url,
                        "duration": duration,
                        "views": views,
                        "channel": channel,
                        "platform": "youtube",
                    })
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            continue

    results.sort(key=lambda v: v.get("views", 0), reverse=True)
    return results[:max_results]


def get_tiktok_trending_es(max_results=20):
    """
    Get trending TikTok videos in Spain using Playwright scraping.
    Requires playwright browsers installed: python -m playwright install chromium
    """
    try:
        from TikTokApi import TikTokApi
    except ImportError:
        print("[WARN] TikTokApi not installed. pip install TikTokApi")
        return []

    results = []
    try:
        with TikTokApi() as api:
            trending = api.trending(count=max_results, region="ES")
            for vid in trending:
                if vid is None:
                    continue
                try:
                    info = vid.as_dict if hasattr(vid, "as_dict") else vid
                    if isinstance(info, dict):
                        results.append({
                            "id": info.get("id", ""),
                            "title": info.get("desc", "") or info.get("title", ""),
                            "url": f"https://tiktok.com/@{info.get('author', {}).get('uniqueId', '')}/video/{info.get('id', '')}",
                            "duration": info.get("video", {}).get("duration", 0) or 0,
                            "views": info.get("stats", {}).get("playCount", 0) or 0,
                            "channel": info.get("author", {}).get("nickname", "") or info.get("author", {}).get("uniqueId", ""),
                            "platform": "tiktok",
                        })
                except Exception:
                    continue
    except Exception as e:
        print(f"[WARN] TikTok trending failed: {e}")

    return results[:max_results]


def get_trending_videos(source="all", max_results=20):
    """
    Get trending videos from specified source(s).

    Args:
        source: "youtube", "tiktok", or "all"
        max_results: max videos per source

    Returns:
        List of video dicts sorted by views descending
    """
    results = []

    if source in ("youtube", "all"):
        yt = get_youtube_trending_es(max_results)
        for v in yt:
            v["source"] = "youtube"
        results.extend(yt)

    if source in ("tiktok", "all"):
        tt = get_tiktok_trending_es(max_results)
        for v in tt:
            v["source"] = "tiktok"
        results.extend(tt)

    results.sort(key=lambda v: v.get("views", 0), reverse=True)
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trending Video Finder")
    parser.add_argument("--source", default="all", choices=["youtube", "tiktok", "all"])
    parser.add_argument("--max", type=int, default=15)
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    videos = get_trending_videos(args.source, args.max)

    if args.json:
        print(json.dumps(videos, indent=2, ensure_ascii=False))
    else:
        for i, v in enumerate(videos):
            dur = f"{v.get('duration', 0)}s" if v.get('duration') else "?"
            views = f"{v.get('views', 0)//1000}K" if v.get('views') else "?"
            source_tag = f"[{v['source'][:2].upper()}]"
            print(f"{i+1}. {source_tag} {v['title'][:70]}")
            print(f"   {views} views | {dur} | {v.get('channel', '')}")
            print(f"   {v['url']}")
