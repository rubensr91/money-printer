"""
Subtitle Engine — Whisper GPU transcription + ffmpeg ASS subtitle burn.

Reliable ffmpeg subtitles filter replaces buggy MoviePy TextClip compositing.
ASS format supports background box (opaque black), proper positioning, and
stroke rendering without edge clipping.
"""

import os
import uuid
import logging

logger = logging.getLogger(__name__)

# ── Transcription ───────────────────────────────────────────────────────────

def transcribe(audio_path: str, languages: list[str] | None = None) -> list[dict]:
    """Transcribe audio with faster-whisper (GPU). Returns entries with start, end, text."""
    from faster_whisper import WhisperModel
    from config import get_whisper_model, get_whisper_device, get_whisper_compute_type
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0

    model = WhisperModel(
        get_whisper_model(), device=get_whisper_device(),
        compute_type=get_whisper_compute_type(),
    )

    entries = []
    if not languages:
        segments, info = model.transcribe(audio_path, vad_filter=True, word_timestamps=True)
        fallback_lang = getattr(info, "language", None) or "es"
        for seg in segments:
            txt = (seg.text or "").strip()
            if not txt:
                continue
            lang = fallback_lang
            try:
                lang = detect(txt)
            except Exception:
                pass
            entries.append({"start": seg.start, "end": seg.end, "text": txt, "language": lang})
    elif len(languages) == 1:
        lang = languages[0]
        segments, _ = model.transcribe(audio_path, vad_filter=True, language=lang, word_timestamps=True)
        for seg in segments:
            txt = (seg.text or "").strip()
            if txt:
                entries.append({"start": seg.start, "end": seg.end, "text": txt, "language": lang})
    else:
        for lang_code in languages:
            try:
                segments, _ = model.transcribe(audio_path, vad_filter=True, language=lang_code, word_timestamps=True)
                for seg in segments:
                    txt = (seg.text or "").strip()
                    if txt:
                        entries.append({"start": seg.start, "end": seg.end, "text": txt, "language": lang_code})
            except Exception as e:
                logger.warning(f"Transcription failed for {lang_code}: {e}")

    entries.sort(key=lambda e: e["start"])
    logger.info(f"Subtitle engine: {len(entries)} entries transcribed")
    return entries


# ── ASS subtitle format ────────────────────────────────────────────────────

def _fmt_ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format: H:MM:SS.CC"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    c = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def entries_to_ass(
    entries: list[dict],
    video_w: int = 1080,
    video_h: int = 1920,
    font_size: int = 70,
    margin_bottom: int = 140,
) -> str:
    """Convert subtitle entries to ASS format with TikTok styling.

    ASS BorderStyle=3 gives an opaque background box behind the text
    (semi-transparent black), which is impossible with plain SRT.

    Args:
        entries: list of {start, end, text} from transcribe()
        video_w, video_h: output video dimensions (1080x1920 for TikTok)
        font_size: font size in points at PlayRes resolution
        margin_bottom: distance from bottom edge (in PlayRes pixels)
    """
    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},&H00FFFFFF,&H00000000,&H80000000,-1,0,3,0,0,2,30,30,{margin_bottom},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    for entry in entries:
        start = _fmt_ass_time(entry["start"])
        end = _fmt_ass_time(entry["end"])
        text = entry["text"].strip().upper()
        ass += f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n"

    return ass


# ── ffmpeg subtitle burn ───────────────────────────────────────────────────

def burn_subtitles(
    video_path: str,
    ass_path: str,
    output_path: str,
    encoder: str = "h264_nvenc",
    fps: int = 30,
) -> str:
    """Burn ASS subtitles into video using ffmpeg-python.

    The video is upscaled from half-res (540x960) to full (1080x1920)
    and subtitles are burned at 1080p resolution.

    Args:
        video_path: input video (540x960 half-res render)
        ass_path: ASS subtitle file
        output_path: final output (1080x1920 with burned subtitles)
        encoder: video codec (h264_nvenc, libx264, etc.)
        fps: output frame rate
    """
    import ffmpeg
    import logging
    log = logging.getLogger(__name__)

    # Normalize paths for ffmpeg on Windows (forward slashes, escape properly)
    video_path = video_path.replace("\\", "/")
    ass_path = ass_path.replace("\\", "/")
    output_path = output_path.replace("\\", "/")

    in_file = ffmpeg.input(video_path)
    scaled = ffmpeg.filter(in_file, "scale", 1080, 1920)
    subbed = ffmpeg.filter(scaled, "subtitles", ass_path)

    args = {"vcodec": encoder, "acodec": "aac", "r": fps}
    if encoder == "h264_nvenc":
        args["preset"] = "p4"

    try:
        stream = ffmpeg.output(subbed, output_path, **args)
        stdout, stderr = ffmpeg.run(stream, overwrite_output=True,
                                     capture_stdout=True, capture_stderr=True)
    except ffmpeg.Error as e:
        log.error(f"ffmpeg subtitle burn failed:\n{e.stderr.decode() if e.stderr else 'no stderr'}")
        raise RuntimeError(f"Subtitle burn failed: {e.stderr.decode()[:300] if e.stderr else 'unknown'}") from e

    logger.info(f"Burned subtitles: {output_path}")
    return output_path


# ── Convenience: extract audio segment ─────────────────────────────────────

def extract_audio_segment(video_path: str, clip_start: float, clip_end: float, output_dir: str) -> str:
    """Extract audio as 16kHz mono WAV for Whisper."""
    import subprocess

    tmp = os.path.join(output_dir, f"_subs_{uuid.uuid4().hex[:8]}.wav")
    # Use the project's bundled ffmpeg
    from tiktok_clips import _find_local_ffmpeg
    ffmpeg_bin = _find_local_ffmpeg()

    subprocess.run([
        ffmpeg_bin, "-y", "-i", video_path,
        "-ss", str(clip_start), "-to", str(clip_end),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", tmp,
    ], check=True, capture_output=True)
    return tmp
