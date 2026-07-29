#!/usr/bin/env python
"""
Auto TikTok Pipeline: trending → clips → descriptions → draft upload → Telegram
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from trending import get_trending_videos
from tiktok_clips import main as generate_clips
from tiktok_uploader import upload_video
from telegram_notify import send_notification
from termcolor import colored


def info(msg):
    print(colored(f"[INFO] {msg}", "blue"))


def ok(msg):
    print(colored(f"[OK] {msg}", "green"))


def warn(msg):
    print(colored(f"[WARN] {msg}", "yellow"))


def run_auto(source="youtube", max_trending=10, clips_per_video=4, min_clip=20, max_clip=59, upload=False):
    """Discover trending videos and generate TikTok clips."""

    info(f"Fetching trending videos from {source}...")
    videos = get_trending_videos(source, max_trending)

    if not videos:
        print("No trending videos found.")
        return

    print(f"\n{'='*60}")
    print(colored(f"  TRENDING VIDEOS ({len(videos)} found)", "cyan"))
    print(f"{'='*60}")
    for i, v in enumerate(videos):
        dur = f"{v.get('duration', 0)}s" if v.get("duration") else "?"
        views = f"{v.get('views', 0)//1000}K" if v.get("views") else "?"
        print(f"  {i+1}. [{views}] {v['title'][:65]}")
        print(f"     {v.get('channel', '')} | {dur} | {v['source']}")

    print(f"\n  Type number to process, 'a' for ALL, 'q' to quit")
    choice = input("  > ").strip().lower()

    if choice == "q":
        return

    if choice == "a":
        selected = videos
    else:
        try:
            indices = [int(c.strip()) - 1 for c in choice.split(",") if c.strip().isdigit()]
            selected = [videos[i] for i in indices if 0 <= i < len(videos)]
        except (ValueError, IndexError):
            print("Invalid selection.")
            return

    if not selected:
        print("No videos selected.")
        return

    all_uploads = []

    for v in selected:
        print(f"\n{'='*60}")
        info(f"Processing: {v['title'][:60]}")
        print(f"  URL: {v['url']}")

        try:
            clips = generate_clips(v["url"], min_clip, max_clip, clips_per_video, with_metadata=True)
        except Exception as e:
            print(colored(f"  [ERROR] {e}", "red"))
            continue

        print(f"\n  Generated {len(clips)} clips:")
        for i, clip in enumerate(clips):
            dur = clip.get("duration", "?")
            desc = clip.get("desc", "")
            tags = clip.get("tags", [])
            tag_str = " ".join(f"#{t}" for t in tags)
            print(f"\n  Clip {i+1} ({dur:.0f}s): {os.path.basename(clip['path'])}")
            print(f"  Desc: {desc}")
            print(f"  Tags: {tag_str}")

            if upload:
                info(f"  Uploading clip {i+1} as draft...")
                try:
                    upload_video(clip["path"], desc, tags, draft=True)
                    ok(f"  Clip {i+1} uploaded as draft")
                    all_uploads.append({"name": os.path.basename(clip["path"]), "desc": desc, "tags": tags})
                except Exception as e:
                    warn(f"  Upload failed: {e}")

    print(f"\n{'='*60}")

    if all_uploads:
        ok(f"Uploaded {len(all_uploads)} clips as drafts")

        msg_parts = ["🎬 <b>Nuevos drafts en TikTok</b>\n"]
        for i, u in enumerate(all_uploads):
            tag_str = " ".join(f"#{t}" for t in u["tags"])
            msg_parts.append(f"<b>Clip {i+1}:</b> {u['desc']}")
            msg_parts.append(f"  {tag_str}")
        msg = "\n".join(msg_parts)

        success = send_notification(msg)
        if success:
            ok("Telegram notification sent")
        else:
            warn("Telegram not configured. Configure with:")
            print("  python -m src.telegram_notify --setup TOKEN CHAT_ID")

    ok("All done!")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Auto TikTok Pipeline")
    parser.add_argument("--source", default="youtube", choices=["youtube", "tiktok", "all"])
    parser.add_argument("--trending", type=int, default=10, help="Videos to fetch")
    parser.add_argument("--clips", type=int, default=4, help="Clips per video")
    parser.add_argument("--min", type=int, default=20, help="Min clip seconds")
    parser.add_argument("--max", type=int, default=59, help="Max clip seconds")
    parser.add_argument("--upload", action="store_true", help="Subir clips como borrador a TikTok (default: no)")
    parser.add_argument("--url", help="Skip trending, process single URL")
    args = parser.parse_args()

    if args.url:
        clips = generate_clips(args.url, args.min, args.max, args.clips, with_metadata=True)
        for i, clip in enumerate(clips):
            tags_str = " ".join(f"#{t}" for t in clip.get("tags", []))
            print(f"Clip {i+1} ({clip.get('duration', '?')}s): {clip['path']}")
            print(f"  Desc: {clip.get('desc', '')}")
            print(f"  Tags: {tags_str}")
            print()

            if args.upload:
                info(f"Uploading clip {i+1} as draft...")
                try:
                    upload_video(clip["path"], clip.get("desc", ""), clip.get("tags", []), draft=True)
                    ok(f"  Uploaded")
                except Exception as e:
                    warn(f"  Upload failed: {e}")
    else:
        run_auto(args.source, args.trending, args.clips, args.min, args.max, upload=args.upload)
