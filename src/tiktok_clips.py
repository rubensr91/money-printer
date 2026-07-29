"""
TikTok Clip Extractor - MoneyPrinterV2
Downloads YouTube video, finds best viral moments via DeepSeek,
cuts into clips, adds TikTok-style subtitles. Original audio kept.
"""

import os
import sys
import re
import json
import uuid
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
from moviepy import (
    VideoFileClip,
    AudioFileClip,
    CompositeVideoClip,
    TextClip,
    vfx,
    afx,
    concatenate_videoclips,
)

from config import (
    ROOT_DIR,
    get_font,
    get_fonts_dir,
    get_whisper_model,
    get_whisper_device,
    get_whisper_compute_type,
    get_threads,
    assert_folder_structure,
)
from llm_provider import generate_text, select_model, get_active_model
from config import get_deepseek_model

assert_folder_structure()
select_model(get_deepseek_model())


def info(msg):
    print(colored(f"[INFO] {msg}", "blue"))


def ok(msg):
    print(colored(f"[OK] {msg}", "green"))


def warn(msg):
    print(colored(f"[WARN] {msg}", "yellow"))


def err(msg):
    print(colored(f"[ERROR] {msg}", "red"))


def _fmt_ts(seconds):
    total_millis = max(0, int(round(seconds * 1000)))
    h = total_millis // 3600000
    m = (total_millis % 3600000) // 60000
    s = (total_millis % 60000) // 1000
    ms = total_millis % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def download_youtube(url, output_dir):
    info(f"Downloading: {url}")
    vid = str(uuid.uuid4())[:8]
    template = os.path.join(output_dir, f"{vid}.%(ext)s")
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
        err(f"yt-dlp failed: {result.stderr}")
        raise RuntimeError(result.stderr)
    video_path = os.path.join(output_dir, f"{vid}.mp4")
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")
    ok(f"Downloaded")
    return video_path


def extract_audio(video_path, output_audio):
    info("Extracting audio...")
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", output_audio,
    ], capture_output=True, check=True)
    return output_audio


def transcribe_full(audio_path, language="es"):
    from faster_whisper import WhisperModel

    info("Transcribing with Whisper GPU...")
    model = WhisperModel(
        get_whisper_model(),
        device=get_whisper_device(),
        compute_type=get_whisper_compute_type(),
    )
    segments, _ = model.transcribe(audio_path, vad_filter=True, language=language)
    result = []
    for seg in segments:
        text = str(seg.text).strip()
        if text:
            result.append({"start": seg.start, "end": seg.end, "text": text})
    ok(f"Transcribed {len(result)} segments")
    return result


def find_best_moments(segments, video_duration, min_clip=15, max_clip=60, num_clips=4):
    """Use DeepSeek to find the most viral moments from transcription."""
    info("Analyzing with DeepSeek to find best moments...")

    transcript = ""
    for seg in segments:
        transcript += f"[{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}\n"

    prompt = f"""Analiza esta transcripción de un video de YouTube y dime cuáles son los mejores momentos virales.

REGLAS:
- El video dura {video_duration:.0f} segundos en total
- Quiero entre {num_clips - 1} y {num_clips + 1} clips de entre {min_clip} y {max_clip} segundos cada uno
- Los clips deben capturar los momentos MÁS VIRALES: lo más gracioso, polémico, sorprendente o impactante
- NO cojas clips aburridos, presentaciones, despedidas o relleno
- Cada clip debe ser auto-contenido (se entiende solo)
- Si el video es corto y no hay suficientes momentos, devuelve menos clips

DEVUELVE SOLO ESTE JSON, SIN TEXTO ADICIONAL:
{{
  "clips": [
    {{"start": 12.0, "end": 38.5, "reason": "Momento más gracioso donde insulta al jefe"}},
    {{"start": 45.0, "end": 78.0, "reason": "Confesión más polémica"}}
  ]
}}

Transcripción:
{transcript[:15000]}"""

    response = generate_text(prompt)
    response = response.strip()
    if response.startswith("```"):
        response = re.sub(r"^```\w*\n?", "", response)
        response = re.sub(r"\n?```$", "", response)

    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        bracket = re.search(r"\{.*\}", response, re.DOTALL)
        if bracket:
            try:
                data = json.loads(bracket.group())
            except json.JSONDecodeError:
                err("DeepSeek returned unparseable JSON")
                data = {"clips": []}
        else:
            err("DeepSeek returned no JSON")
            data = {"clips": []}

    clips = data.get("clips", [])

    validated = []
    for c in clips:
        start = float(c.get("start", 0))
        end = float(c.get("end", 0))
        dur = end - start
        if dur < 5 or dur > video_duration:
            continue
        if start < 0:
            start = 0
        if end > video_duration:
            end = video_duration
        validated.append({"start": start, "end": end, "reason": c.get("reason", "")})

    ok(f"DeepSeek found {len(validated)} viral moments")
    for i, v in enumerate(validated):
        print(f"  [{i+1}] {v['start']:.0f}s-{v['end']:.0f}s | {v['reason'][:80]}")

    return validated


def _split_segment_naturally(text, start, end):
    """Split a long transcript segment into natural 4-8 word subtitles."""
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


def _load_face_cascade():
    """Load Haar cascade from temp file (OpenCV 5 compat)."""
    import cv2, tempfile
    cascade_path = os.path.join(os.path.dirname(__file__), "..", ".mp", "haarcascade_frontalface_default.xml")
    if not os.path.exists(cascade_path):
        import urllib.request
        url = "https://raw.githubusercontent.com/opencv/opencv/master/data/haarcascades/haarcascade_frontalface_default.xml"
        urllib.request.urlretrieve(url, cascade_path)
    with open(cascade_path, "rb") as f:
        data = f.read()
    tmp = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
    tmp.write(data)
    tmp.close()
    cascade = cv2.CascadeClassifier(tmp.name)
    return cascade, tmp.name


def find_face_positions(video_path, clip_start, clip_end, segment_dur=2.5):
    """Sample face positions every segment_dur seconds. Returns list of (t, x_ratio)."""
    import cv2
    cascade, tmp_path = _load_face_cascade()
    if cascade.empty():
        os.unlink(tmp_path)
        return [(0, 0.5)]

    cap = cv2.VideoCapture(video_path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    dur = clip_end - clip_start
    num_samples = max(2, int(dur / segment_dur))

    positions = []
    for i in range(num_samples):
        t = clip_start + dur * (i + 0.5) / num_samples
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ret, frame = cap.read()
        if not ret:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, 1.08, 6, minSize=(60, 60))
        if len(faces) > 0:
            best = max(faces, key=lambda f: f[2] * f[3])
            fx = best[0] + best[2] // 2
            positions.append((t - clip_start, fx / w))
        else:
            positions.append((t - clip_start, None))

    cap.release()
    os.unlink(tmp_path)

    if not positions:
        return [(0, 0.5)]

    valid = [(t, x) for t, x in positions if x is not None]
    if not valid:
        return [(0, 0.5)]

    median_x = sorted(x for _, x in valid)[len(valid) // 2]

    smoothed = []
    for t, x in positions:
        if x is None:
            smoothed.append((t, median_x))
        else:
            smoothed.append((t, x))

    if len(smoothed) > 2:
        final = [smoothed[0]]
        for i in range(1, len(smoothed) - 1):
            avg_x = (smoothed[i - 1][1] + smoothed[i][1] + smoothed[i + 1][1]) / 3
            final.append((smoothed[i][0], avg_x))
        final.append(smoothed[-1])
        smoothed = final

    return smoothed


def make_sub_clip(text, w, h, font_path):
    fontsize = int(w * 0.055)
    stroke_w = max(2, fontsize // 10)
    return TextClip(
        text=text.upper(),
        font=font_path,
        font_size=fontsize,
        color="#FFFFFF",
        stroke_color="#000000",
        stroke_width=stroke_w,
        method="caption",
        size=(int(w * 0.9), None),
        text_align="center",
    )


def process_clip(video_path, clip_start, clip_end, segments, clip_idx, output_dir):
    """Process one clip: face-tracking crop per 2.5s segment, natural subtitles, original audio."""
    import cv2

    info(f"Clip {clip_idx}: {clip_start:.0f}s - {clip_end:.0f}s")
    clip_dur = clip_end - clip_start

    face_positions = find_face_positions(video_path, clip_start, clip_end, segment_dur=2.5)
    info(f"  Face tracking: {len(face_positions)} keyframes")

    target_ratio = 9.0 / 16.0

    clip_segments = [
        s for s in segments
        if s["end"] >= clip_start and s["start"] <= clip_end
    ]
    for s in clip_segments:
        s["start"] = max(0, s["start"] - clip_start)
        s["end"] = min(clip_dur, s["end"] - clip_start)

    font_path = os.path.join(get_fonts_dir(), get_font())
    if not os.path.exists(font_path):
        font_path = os.path.join(get_fonts_dir(), "bold_font.ttf")

    # Build sub-clip segments for face tracking
    seg_dur = 2.5
    num_subsegs = max(1, int(clip_dur / seg_dur))
    sub_clips = []
    total_subtitles = 0

    for i in range(num_subsegs):
        t0 = i * clip_dur / num_subsegs
        t1 = (i + 1) * clip_dur / num_subsegs

        # Find interpolated face x for this time window
        face_x = 0.5
        for j in range(len(face_positions) - 1):
            if face_positions[j][0] <= (t0 + t1) / 2 <= face_positions[j + 1][0]:
                frac = ((t0 + t1) / 2 - face_positions[j][0]) / (face_positions[j + 1][0] - face_positions[j][0]) if face_positions[j + 1][0] != face_positions[j][0] else 0
                face_x = face_positions[j][1] + frac * (face_positions[j + 1][1] - face_positions[j][1])
                break
        else:
            if face_positions:
                face_x = face_positions[len(face_positions) // 2][1]

        sub = VideoFileClip(video_path).subclipped(clip_start + t0, clip_start + t1)

        cur_ratio = sub.w / sub.h
        if cur_ratio > target_ratio:
            nw = int(sub.h * target_ratio)
            xc = int(sub.w * face_x)
            xc = max(nw // 2, min(sub.w - nw // 2, xc))
            sub = sub.with_effects([
                vfx.Crop(x_center=xc, y_center=sub.h // 2, width=nw, height=sub.h),
            ])
        else:
            nh = int(sub.w / target_ratio)
            sub = sub.with_effects([
                vfx.Crop(x_center=sub.w // 2, y_center=sub.h // 2, width=sub.w, height=nh),
            ])
        sub = sub.resized((1080, 1920))

        # Subtitles for this sub-segment
        sub_segs = [
            s for s in clip_segments
            if s["end"] > t0 and s["start"] < t1
        ]
        sub_text_clips = []
        for seg in sub_segs:
            natural_chunks = _split_segment_naturally(seg["text"], seg["start"], seg["end"])
            for cs, ce, chunk_text in natural_chunks:
                rel_start = cs - t0
                rel_end = ce - t0
                if rel_end <= 0 or rel_start >= (t1 - t0):
                    continue
                rel_start = max(0, rel_start)
                rel_end = min(t1 - t0, rel_end)
                if rel_end - rel_start < 0.3:
                    continue
                txt_clip = make_sub_clip(chunk_text, sub.w, sub.h, font_path)
                txt_clip = txt_clip.with_start(rel_start)
                txt_clip = txt_clip.with_duration(rel_end - rel_start)
                txt_clip = txt_clip.with_position(("center", sub.h * 0.82))
                sub_text_clips.append(txt_clip)

        if sub_text_clips:
            sub = CompositeVideoClip([sub, *sub_text_clips])
            total_subtitles += len(sub_text_clips)

        sub_clips.append(sub)

    clip = concatenate_videoclips(sub_clips)
    info(f"  Face segments: {num_subsegs}, subtitles: {total_subtitles}")

    if clip.audio is not None:
        clip = clip.with_effects([afx.MultiplyVolume(0.85)])

    output_path = os.path.join(output_dir, f"tiktok_clip_{clip_idx}.mp4")
    threads = get_threads()
    clip.write_videofile(
        output_path,
        codec="libx264",
        audio_codec="aac",
        threads=threads,
        preset="medium",
        fps=30,
    )
    clip.close()
    ok(f"  Saved: {output_path}")
    return output_path


def generate_descriptions(moments, segments):
    """Use DeepSeek to generate viral descriptions and 5 hashtags per clip."""
    info("Generating descriptions with DeepSeek...")

    results = []
    for i, moment in enumerate(moments):
        clip_segs = [
            s for s in segments
            if s["end"] >= moment["start"] and s["start"] <= moment["end"]
        ]
        transcript = "\n".join(
            f'[{s["start"]:.1f}s] {s["text"]}' for s in clip_segs[:15]
        )

        prompt = f"""Eres un experto en TikTok viral en español. Basado en esta transcripcion, genera:
1. Una descripcion viral de max 120 caracteres (sin hashtags en la descripcion)
2. Exactamente 5 hashtags relevantes y virales

IMPORTANTE:
- La descripcion NO debe incluir hashtags
- Los hashtags en linea separada
- Los hashtags sin el simbolo #
- Solo 5 hashtags (limite de TikTok)
- Deben ser hashtags que la gente busca de verdad

Responde exactamente en este formato:
DESC: <descripcion>
TAGS: tag1, tag2, tag3, tag4, tag5

Transcripcion del clip:
{transcript[:2000]}"""

        try:
            resp = generate_text(prompt, model_name=get_deepseek_model())
            desc_match = re.search(r'DESC:\s*(.+?)(?:\n|$)', resp)
            tags_match = re.search(r'TAGS:\s*(.+?)(?:\n|$)', resp)

            desc = desc_match.group(1).strip() if desc_match else "Mira esto 👀"
            tags_str = tags_match.group(1).strip() if tags_match else "viral,humor,españa,tiktok,parati"
            tags = [t.strip().strip("#") for t in tags_str.split(",")][:5]

        except Exception:
            desc = "Mira esto 👀"
            tags = ["viral", "humor", "españa", "tiktok", "parati"]

        results.append({"desc": desc, "tags": tags})

    return results


def main_stream(youtube_url, min_clip=15, max_clip=59, num_clips=4):
    """
    Same as main() but yields each clip as it's generated (streaming).
    Yields: dict with {path, duration, desc, tags, index}
    """
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    os.makedirs(mp_dir, exist_ok=True)

    video_path = download_youtube(youtube_url, mp_dir)

    clip_info = VideoFileClip(video_path)
    video_duration = clip_info.duration
    clip_info.close()

    audio_path = os.path.join(mp_dir, f"{uuid.uuid4()}.wav")
    extract_audio(video_path, audio_path)

    segments = transcribe_full(audio_path, language="es")

    moments = find_best_moments(segments, video_duration, min_clip, max_clip, num_clips)

    if not moments:
        warn("No moments found. Using time-based split as fallback.")
        chunk_dur = min(max_clip, video_duration / num_clips)
        for i in range(num_clips):
            start = i * chunk_dur
            end = min(video_duration, (i + 1) * chunk_dur)
            if end - start >= min_clip:
                moments.append({"start": start, "end": end, "reason": "auto-split"})

    descriptions = generate_descriptions(moments, segments)

    for i, moment in enumerate(moments):
        out = process_clip(video_path, moment["start"], moment["end"], segments, i + 1, mp_dir)
        dur = moment["end"] - moment["start"]
        result = {
            "path": out,
            "duration": dur,
            "index": i + 1,
            "start": moment["start"],
            "end": moment["end"],
            "reason": moment.get("reason", ""),
        }
        if i < len(descriptions):
            result["desc"] = descriptions[i]["desc"]
            result["tags"] = descriptions[i]["tags"]
        else:
            result["desc"] = "Mira esto 👀"
            result["tags"] = ["viral", "humor", "españa", "tiktok", "parati"]
        yield result

    if os.path.exists(audio_path):
        os.remove(audio_path)


def main(youtube_url, min_clip=15, max_clip=59, num_clips=4, with_metadata=True):
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    os.makedirs(mp_dir, exist_ok=True)

    video_path = download_youtube(youtube_url, mp_dir)

    clip_info = VideoFileClip(video_path)
    video_duration = clip_info.duration
    clip_info.close()

    audio_path = os.path.join(mp_dir, f"{uuid.uuid4()}.wav")
    extract_audio(video_path, audio_path)

    segments = transcribe_full(audio_path, language="es")

    moments = find_best_moments(segments, video_duration, min_clip, max_clip, num_clips)

    if not moments:
        warn("No moments found. Using time-based split as fallback.")
        chunk_dur = min(max_clip, video_duration / num_clips)
        for i in range(num_clips):
            start = i * chunk_dur
            end = min(video_duration, (i + 1) * chunk_dur)
            if end - start >= min_clip:
                moments.append({"start": start, "end": end, "reason": "auto-split"})

    if with_metadata:
        descriptions = generate_descriptions(moments, segments)

    outputs = []
    for i, moment in enumerate(moments):
        out = process_clip(video_path, moment["start"], moment["end"], segments, i + 1, mp_dir)
        dur = moment["end"] - moment["start"]
        result = {"path": out, "duration": dur}
        if with_metadata and i < len(descriptions):
            result.update(descriptions[i])
        outputs.append(result)

    if os.path.exists(audio_path):
        os.remove(audio_path)

    return outputs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TikTok Clip Extractor")
    parser.add_argument("url", nargs="?", help="YouTube URL")
    parser.add_argument("--min", type=int, default=15, help="Min clip seconds")
    parser.add_argument("--max", type=int, default=59, help="Max clip seconds")
    parser.add_argument("--clips", type=int, default=4, help="Number of clips")
    args = parser.parse_args()

    url = args.url
    if not url:
        url = input("YouTube URL: ").strip()
    if not url:
        err("No URL")
        sys.exit(1)

    outputs = main(url, args.min, args.max, args.clips)
    print(f"\n{'='*50}")
    for o in outputs:
        print(o)
