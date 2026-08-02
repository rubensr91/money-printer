"""Send any video to Telegram directly (bypasses bot worker). Useful for manual renders, summaries, subtitle jobs.

Usage:
  python scripts/send_telegram.py <video_path> [caption]
  python -c "from scripts.send_telegram import send; send('clip.mp4', '🎬 Clip!')"

Config: reads .mp/telegram.json {bot_token, chat_id}
Max size: 50MB (Telegram Bot API limit). Use -c copy (no re-encode) where possible.
"""
import os, sys, json, requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, ".mp", "telegram.json")))

def send(video_path: str, caption: str = "", parse_mode: str = "HTML") -> bool:
    """Send a video to Telegram. Returns True if OK."""
    if not os.path.exists(video_path):
        print(f"ERROR: file not found: {video_path}", file=sys.stderr)
        return False
    sz = os.path.getsize(video_path) / 1024 / 1024
    if sz > 50:
        print(f"WARN: {sz:.1f}MB > 50MB limit, may fail", file=sys.stderr)
    with open(video_path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{CFG['bot_token']}/sendVideo",
            data={
                "chat_id": CFG["chat_id"],
                "caption": caption,
                "parse_mode": parse_mode,
                "supports_streaming": True,
            },
            files={"video": (os.path.basename(video_path), f, "video/mp4")},
            timeout=180,
        )
    ok = r.json().get("ok", False)
    print(f"Telegram: {'OK' if ok else r.text[:200]}")
    return ok


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <video.mp4> [caption]", file=sys.stderr)
        sys.exit(1)
    caption = sys.argv[2] if len(sys.argv) > 2 else ""
    ok = send(sys.argv[1], caption)
    sys.exit(0 if ok else 1)
