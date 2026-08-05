"""
TikTok Video Creator - MoneyPrinterV2
Downloads YouTube clips, transcribes, adds TikTok-style subtitles.
No voiceover. Subtitles only. Clean minimal style.
"""

import os
import sys
import uuid
import subprocess
import random
from pathlib import Path

_venv_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_cuda_dirs = [
    os.path.join(_venv_dir, "Lib", "site-packages", "nvidia", "cublas", "bin"),
    os.path.join(_venv_dir, "Lib", "site-packages", "nvidia", "cudnn", "bin"),
    os.path.join(_venv_dir, "Lib", "site-packages", "nvidia", "cuda_nvrtc", "bin"),
    os.path.join(_venv_dir, "Lib", "site-packages", "nvidia", "cuda_runtime", "bin"),
    os.path.join(_venv_dir, "Lib", "site-packages", "ctranslate2"),
]
for _d in _cuda_dirs:
    if os.path.isdir(_d):
        os.add_dll_directory(_d)

from termcolor import colored

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    ROOT_DIR,
    get_verbose,
    get_font,
    get_fonts_dir,
    get_whisper_model,
    get_whisper_device,
    get_whisper_compute_type,
    get_threads,
    assert_folder_structure,
)

from moviepy import (
    VideoFileClip,
    AudioFileClip,
    CompositeVideoClip,
    TextClip,
    vfx,
    afx,
)

assert_folder_structure()


def info(msg: str):
    print(colored(f"[INFO] {msg}", "blue"))


def success(msg: str):
    print(colored(f"[OK] {msg}", "green"))


def warning(msg: str):
    print(colored(f"[WARN] {msg}", "yellow"))


def error(msg: str):
    print(colored(f"[ERROR] {msg}", "red"))


def _format_srt_timestamp(seconds: float) -> str:
    total_millis = max(0, int(round(seconds * 1000)))
    hours = total_millis // 3600000
    minutes = (total_millis % 3600000) // 60000
    secs = (total_millis % 60000) // 1000
    millis = total_millis % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def download_youtube(url: str, output_dir: str) -> tuple[str, str]:
    info(f"Downloading: {url}")

    video_id = str(uuid.uuid4())[:8]
    template = os.path.join(output_dir, f"{video_id}.%(ext)s")

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
        "-o", template,
        "--merge-output-format", "mp4",
        "--no-playlist",
        url,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        error(f"yt-dlp failed: {result.stderr}")
        raise RuntimeError(result.stderr)

    title = "video"

    video_path = os.path.join(output_dir, f"{video_id}.mp4")
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found at {video_path}")

    success(f"Downloaded: {title}")
    return video_path, title


def extract_audio(video_path: str, output_audio: str) -> str:
    info("Extracting audio...")
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000",
        "-ac", "1", output_audio,
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return output_audio




def parse_srt(srt_path: str) -> list[tuple[float, float, str]]:
    sub_list = []
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = content.strip().split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        time_line = lines[1]
        text = " ".join(lines[2:])
        try:
            start_str, end_str = time_line.split(" --> ")
            start = _parse_time(start_str)
            end = _parse_time(end_str)
            sub_list.append((start, end, text))
        except Exception:
            continue

    return sub_list


def _parse_time(ts: str) -> float:
    ts = ts.replace(",", ".")
    h, m, s = ts.split(":")
    return float(h) * 3600 + float(m) * 60 + float(s)


def make_subtitle_clip(
    text: str,
    video_width: int,
    video_height: int,
    font_path: str,
) -> TextClip:
    fontsize = int(video_width * 0.055)
    stroke_w = max(2, fontsize // 10)

    return TextClip(
        text=text.upper(),
        font=font_path,
        font_size=fontsize,
        color="#FFFFFF",
        stroke_color="#000000",
        stroke_width=stroke_w,
        method="caption",
        size=(int(video_width * 0.9), None),
        text_align="center",
    )


def add_tiktok_subtitles(
    video: VideoFileClip,
    srt_path: str,
    font_path: str,
) -> CompositeVideoClip:
    """DEPRECATED: use subtitle_engine.render_subtitles() instead.
    Kept for backward compatibility with direct tiktok_video.py usage."""
    subs = parse_srt(srt_path)

    sub_clips = []
    for start, end, text in subs:
        if not text.strip():
            continue
        txt_clip = make_subtitle_clip(text, video.w, video.h, font_path)
        txt_clip = txt_clip.with_start(start)
        txt_clip = txt_clip.with_duration(end - start)
        txt_clip = txt_clip.with_position(("center", video.h * 0.82))
        sub_clips.append(txt_clip)

    info(f"Added {len(sub_clips)} subtitle clips")
    return CompositeVideoClip([video, *sub_clips])


def process_video(
    youtube_url: str,
    output_path: str = None,
    music_dir: str = None,
    mute_original: bool = True,
) -> str:
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    os.makedirs(mp_dir, exist_ok=True)

    video_path, title = download_youtube(youtube_url, mp_dir)
    info(f"Processing: {title}")

    if output_path is None:
        video_id = str(uuid.uuid4())[:8]
        output_path = os.path.join(mp_dir, f"tiktok_{video_id}.mp4")

    clip = VideoFileClip(video_path)

    audio_path = os.path.join(mp_dir, f"{uuid.uuid4()}.wav")
    extract_audio(video_path, audio_path)

    srt_path = os.path.join(mp_dir, f"{uuid.uuid4()}.srt")
    transcribe_audio(audio_path, srt_path, language="es")

    info("Cropping to 9:16 TikTok format...")
    target_ratio = 9.0 / 16.0
    current_ratio = clip.w / clip.h

    if current_ratio > target_ratio:
        new_width = int(clip.h * target_ratio)
        clip = clip.with_effects([
            vfx.Crop(x_center=clip.w / 2, y_center=clip.h / 2, width=new_width, height=clip.h),
        ])
    else:
        new_height = int(clip.w / target_ratio)
        clip = clip.with_effects([
            vfx.Crop(x_center=clip.w / 2, y_center=clip.h / 2, width=clip.w, height=new_height),
        ])

    clip = clip.resized((1080, 1920))

    if mute_original:
        if music_dir and os.path.isdir(music_dir):
            songs = [
                f for f in os.listdir(music_dir)
                if f.lower().endswith((".mp3", ".wav", ".m4a"))
            ]
            if songs:
                song_path = os.path.join(music_dir, random.choice(songs))
                info(f"Adding music: {os.path.basename(song_path)}")
                music_clip = AudioFileClip(song_path)
                if music_clip.duration < clip.duration:
                    music_clip = music_clip.with_duration(clip.duration)
                else:
                    music_clip = music_clip.subclipped(0, clip.duration)
                music_clip = music_clip.with_effects([afx.MultiplyVolume(0.3)])
                clip = clip.with_audio(music_clip)
            else:
                clip = clip.without_audio()
        else:
            clip = clip.without_audio()

    clip = clip.with_duration(clip.duration)

    threads = get_threads()
    info(f"Rendering {output_path} (threads={threads})...")
    clip.write_videofile(
        output_path,
        codec="libx264",
        audio_codec="aac",
        threads=threads,
        preset="medium",
        fps=30,
    )

    clip.close()
    success(f"Video saved: {output_path}")

    for tmp in [audio_path, srt_path]:
        if os.path.exists(tmp):
            os.remove(tmp)

    return output_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TikTok Video Creator")
    parser.add_argument("url", nargs="?", help="YouTube video URL")
    parser.add_argument("-o", "--output", help="Output video path")
    parser.add_argument("-m", "--music", help="Music directory path")
    parser.add_argument("--keep-audio", action="store_true", help="Keep original audio")
    args = parser.parse_args()

    url = args.url
    if not url:
        url = input("Enter YouTube URL: ").strip()

    if not url:
        error("No URL provided")
        sys.exit(1)

    music_dir = args.music
    if not music_dir:
        songs_dir = os.path.join(ROOT_DIR, "Songs")
        if os.path.isdir(songs_dir) and os.listdir(songs_dir):
            music_dir = songs_dir

    process_video(
        url,
        output_path=args.output,
        music_dir=music_dir,
        mute_original=not args.keep_audio,
    )
