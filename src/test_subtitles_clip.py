"""
Test: SubtitlesClip de MoviePy.
Genera clip de 40s con subtítulos al estilo película (abajo).
Fixes: Arial (tildes OK), SRT partido en chunks cortos, position abajo.
"""

import os, sys, uuid, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from termcolor import colored
from moviepy import (
    VideoFileClip, CompositeVideoClip,
    TextClip, vfx, afx,
)
from moviepy.video.tools.subtitles import SubtitlesClip

from config import ROOT_DIR
from subtitle_engine import transcribe_segment

def info(msg): print(colored(f"[INFO] {msg}", "blue"))
def ok(msg):   print(colored(f"[OK] {msg}", "green"))
def err(msg):  print(colored(f"[ERROR] {msg}", "red"))

def _fmt_ts(seconds):
    total_millis = max(0, int(round(seconds * 1000)))
    h = total_millis // 3600000
    m = (total_millis % 3600000) // 60000
    s = (total_millis % 60000) // 1000
    ms = total_millis % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _split_segment_naturally(text, start, end):
    """Split text into natural 4-8 word chunks with proportional timing."""
    words = text.split()
    n = len(words)
    if n <= 6:
        return [(start, end, text)]
    target = max(4, min(8, n // max(1, int((end - start) * 1.5))))
    chunks = []
    i = 0
    while i < n:
        size = min(target, n - i)
        chunk = " ".join(words[i : i + size])
        cs = start + (end - start) * (i / n)
        ce = start + (end - start) * ((i + size) / n)
        chunks.append((cs, ce, chunk))
        i += size
    return chunks

def segments_to_srt_chunked(segments, output_path):
    """Convert whisper segments to SRT, splitting long text into chunks."""
    lines = []
    idx = 1
    for seg in segments:
        chunks = _split_segment_naturally(seg["text"], seg["start"], seg["end"])
        for cs, ce, text in chunks:
            lines.append(str(idx))
            lines.append(f"{_fmt_ts(cs)} --> {_fmt_ts(ce)}")
            lines.append(text)
            lines.append("")
            idx += 1
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return output_path

def test_subtitles_clip():
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    out_path = os.path.join(mp_dir, "test_subtitles_output.mp4")

    source_path = os.path.join(mp_dir, "12376307.mp4")
    if not os.path.exists(source_path):
        err(f"Source not found: {source_path}")
        return

    info(f"Source: {source_path}")

    # Extraer audio
    clip_duration = 40
    temp_audio = os.path.join(mp_dir, f"test_{uuid.uuid4().hex[:8]}.wav")
    info("Extracting audio (first 40s)...")
    subprocess.run([
        "ffmpeg", "-y", "-i", source_path,
        "-t", str(clip_duration),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", temp_audio,
    ], capture_output=True, check=True)

    # Transcribir
    info("Transcribing with Whisper GPU...")
    segments = transcribe_segment(temp_audio, languages=["es"])

    # Crear SRT con chunks cortos
    srt_path = os.path.join(mp_dir, "test_subtitles.srt")
    segments_to_srt_chunked(segments, srt_path)
    info(f"SRT: {srt_path}")

    # SRT chunk count
    with open(srt_path, "r", encoding="utf-8") as f:
        srt_lines = f.read().strip().split("\n\n")
    info(f"  {len(srt_lines)} subtitle entries (chunked)")

    # Font: Arial (soporta tildes, reliable)
    arial_path = "C:/Windows/Fonts/arial.ttf"
    if not os.path.exists(arial_path):
        err("Arial not found!")
        return
    font_path = arial_path
    info(f"Font: Arial")

    # Cargar video
    info("Loading video...")
    video = VideoFileClip(source_path).subclipped(0, clip_duration)

    # Crop 9:16 centrado
    target_ratio = 9.0 / 16.0
    cur_ratio = video.w / video.h
    if cur_ratio > target_ratio:
        nw = int(video.h * target_ratio)
        xc = video.w // 2
        video = video.with_effects([
            vfx.Crop(x_center=xc, y_center=video.h // 2, width=nw, height=video.h),
        ])
    else:
        nh = int(video.w / target_ratio)
        video = video.with_effects([
            vfx.Crop(x_center=video.w // 2, y_center=video.h // 2, width=video.w, height=nh),
        ])
    video = video.resized((1080, 1920))
    info(f"Video: {video.w}x{video.h}, {video.duration:.1f}s")

    # Generador: caption con altura fija (padding vertical evita corte)
    font_size = 52
    generator = lambda txt: TextClip(
        text=txt,
        font=font_path,
        font_size=font_size,
        color="#FFFFFF",
        stroke_color="#000000",
        stroke_width=3,
        method="caption",
        size=(1000, 80),  # ancho generoso, alto fijo con padding
        text_align="center",
    )

    # Subtítulos — encoding UTF-8 obligatorio para tildes
    info("Creating SubtitlesClip...")
    subtitles = SubtitlesClip(srt_path, make_textclip=generator, encoding="utf-8")
    # Posición: abajo estilo película
    bottom_margin = 150
    subtitles = subtitles.with_position(("center", video.h - bottom_margin))

    # Componer
    info("Compositing...")
    final = CompositeVideoClip([video, subtitles])

    # Audio
    if final.audio is not None:
        final = final.with_effects([afx.MultiplyVolume(0.85)])

    # Render
    info(f"Rendering to {out_path}...")
    final.write_videofile(
        out_path,
        codec="libx264",
        audio_codec="aac",
        threads=8,
        preset="fast",
        fps=30,
    )
    final.close()
    ok(f"Done: {out_path}")

    if os.path.exists(temp_audio):
        os.remove(temp_audio)

if __name__ == "__main__":
    test_subtitles_clip()
