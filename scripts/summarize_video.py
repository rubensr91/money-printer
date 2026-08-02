"""Generate a horizontal summary from a YouTube video using IA moment selection.

Workflow:
  1. Download video + VTT captions (yt-dlp)
  2. Parse captions → send to DeepSeek to pick key moments (~5 min)
  3. Concat selected segments with ffmpeg -c copy (no re-encode, instant)
  4. Output: horizontal MP4, original quality, no background/subtitles

Usage:
  python scripts/summarize_video.py <youtube_url> [--duration 300] [--send]

Dependencies: yt-dlp, faster-whisper (optional), DeepSeek API key (config.json)
"""
import os, sys, subprocess, argparse, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MP = os.path.join(ROOT, ".mp")
os.makedirs(MP, exist_ok=True)

# ── helpers ──────────────────────────────────────────────────────────────────
def _find_ffmpeg():
    """Look for bundled ffmpeg first, fall back to PATH."""
    bundled = os.path.join(ROOT, ".mp", "tools", "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled
    for name in ("ffmpeg", "ffmpeg.exe"):
        if subprocess.run(["where", name], capture_output=True).returncode == 0:
            return name
    raise RuntimeError("ffmpeg not found — install or put in .mp/tools/")

FFMPEG = _find_ffmpeg()
FFPROBE = FFMPEG.replace("ffmpeg", "ffprobe")

# ── download ─────────────────────────────────────────────────────────────────
def download_video(url: str, video_id: str) -> str:
    """Download video + auto-subs, return path to merged mp4."""
    out_tmpl = os.path.join(MP, "%(id)s.%(ext)s")
    # subs
    subprocess.run([
        sys.executable, "-m", "yt_dlp",
        "--write-auto-subs", "--sub-lang", "es,en",
        "-o", out_tmpl, "--no-playlist", url,
    ], check=True, capture_output=True)
    # video
    subprocess.run([
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "--merge-output-format", "mp4",
        "-o", out_tmpl, "--no-playlist", url,
    ], check=True, capture_output=True)
    return os.path.join(MP, f"{video_id}.mp4"), os.path.join(MP, f"{video_id}.es.vtt") or os.path.join(MP, f"{video_id}.en.vtt")

# ── IA moment selection ──────────────────────────────────────────────────────
def select_moments(vtt_path: str, video_path: str, target_s: int) -> list[tuple[int,int]]:
    """Parse VTT → DeepSeek picks key moments totaling ~target_s seconds."""
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from tiktok_clips import parse_vtt, find_best_moments
    from moviepy import VideoFileClip

    segs = parse_vtt(vtt_path)
    v = VideoFileClip(video_path)
    dur = v.duration
    v.close()
    print(f"Analyzing {len(segs)} caption segments ({dur:.0f}s video)...")

    n_clips = max(4, min(12, target_s // 30))
    instructions = (
        f"RESUMEN DE {target_s//60} MINUTOS: Selecciona los momentos más importantes "
        f"del video para crear un resumen de ~{target_s} segundos. "
        "Prioriza información clave, puntos principales y conclusiones. "
        "Evita pausas, repeticiones y relleno."
    )
    moments, desc, tags = find_best_moments(segs, dur, min_clip=10, max_clip=90,
                                            num_clips=n_clips, instructions=instructions)
    total = sum(m["end"] - m["start"] for m in moments)
    print(f"Selected {len(moments)} moments, {total:.0f}s total")
    for i, m in enumerate(moments):
        print(f"  {i+1}. {m['start']:.0f}s → {m['end']:.0f}s  ({m.get('title','')[:70]})")
    return [(int(m["start"]), int(m["end"])) for m in moments], desc, tags

# ── concat render ────────────────────────────────────────────────────────────
def concat_segments(video_path: str, moments: list[tuple[int,int]], output_path: str) -> str:
    """Concat segments without re-encoding (ffmpeg -c copy, instant)."""
    txt = os.path.join(MP, "_concat_list.txt")
    with open(txt, "w", encoding="utf-8") as f:
        for s, e in moments:
            f.write(f"file '{video_path}'\ninpoint {s}\noutpoint {e}\n")

    subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", txt,
                    "-c", "copy", "-movflags", "+faststart", output_path],
                   check=True, capture_output=True)
    dur = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                                output_path], capture_output=True, text=True).stdout.strip())
    sz = os.path.getsize(output_path) / 1024 / 1024
    print(f"Summary: {dur:.0f}s, {sz:.1f}MB ({output_path})")
    os.remove(txt)
    return output_path

# ── main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="AI-powered video summarizer")
    parser.add_argument("url", help="YouTube URL")
    parser.add_argument("--duration", type=int, default=300, help="Target summary seconds (default: 300)")
    parser.add_argument("--send", action="store_true", help="Send to Telegram after render")
    args = parser.parse_args()

    # Extract video ID
    import re
    m = re.search(r"(?:v=|/)([\w-]{11})", args.url)
    vid_id = m.group(1) if m else "video"
    vid_path = os.path.join(MP, f"{vid_id}.mp4")
    vtt_path = os.path.join(MP, f"{vid_id}.es.vtt")

    # Download if not cached
    if not os.path.exists(vid_path):
        print("Downloading video + captions...")
        vid_path, vtt = download_video(args.url, vid_id)
        if os.path.exists(vtt):
            vtt_path = vtt
    else:
        print(f"Using cached: {vid_path}")

    if not os.path.exists(vtt_path):
        # Fallback to .en
        en = os.path.join(MP, f"{vid_id}.en.vtt")
        if os.path.exists(en):
            vtt_path = en
        else:
            print("WARN: No VTT found, using time-based split")
            moments = [(0, args.duration)]
            desc = tags = ""
    else:
        moments, desc, tags = select_moments(vtt_path, vid_path, args.duration)

    out = os.path.join(MP, f"{vid_id}_summary.mp4")
    concat_segments(vid_path, moments, out)

    if args.send:
        from send_telegram import send
        caption = f"🎬 <b>Resumen IA ~{args.duration//60} min</b>"
        if desc:
            caption += f"\n\n{desc[:300]}"
        send(out, caption)

if __name__ == "__main__":
    main()
