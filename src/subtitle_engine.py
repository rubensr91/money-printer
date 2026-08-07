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
    """Transcribe audio with faster-whisper (GPU).

    Uses word timestamps to trim leading/trailing silence and split long
    segments at natural pauses (>0.8s gap between words) so captions stay
    tightly locked to speech instead of lingering in silence.
    """
    from faster_whisper import WhisperModel
    from config import get_whisper_model, get_whisper_device, get_whisper_compute_type
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0

    model = WhisperModel(
        get_whisper_model(), device=get_whisper_device(),
        compute_type=get_whisper_compute_type(),
    )

    raw_entries = []
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
            raw_entries.append({"start": seg.start, "end": seg.end, "text": txt,
                                "language": lang, "words": seg.words or []})
    elif len(languages) == 1:
        lang = languages[0]
        segments, _ = model.transcribe(audio_path, vad_filter=True, language=lang, word_timestamps=True)
        for seg in segments:
            txt = (seg.text or "").strip()
            if txt:
                raw_entries.append({"start": seg.start, "end": seg.end, "text": txt,
                                    "language": lang, "words": seg.words or []})
    else:
        for lang_code in languages:
            try:
                segments, _ = model.transcribe(audio_path, vad_filter=True, language=lang_code, word_timestamps=True)
                for seg in segments:
                    txt = (seg.text or "").strip()
                    if txt:
                        raw_entries.append({"start": seg.start, "end": seg.end, "text": txt,
                                            "language": lang_code, "words": seg.words or []})
            except Exception as e:
                logger.warning(f"Transcription failed for {lang_code}: {e}")

    # Tighten timestamps: trim silence using word boundaries, split at pauses
    entries = _tighten_entries(raw_entries)
    entries.sort(key=lambda e: e["start"])
    logger.info(f"Subtitle engine: {len(entries)} entries transcribed (tightened)")
    return entries


def _tighten_entries(raw_entries: list[dict]) -> list[dict]:
    """Trim silence and split long segments using word timestamps."""
    result = []
    for seg in raw_entries:
        words = [w for w in seg.get("words", []) if (w.word or "").strip()]
        if not words:
            # No word timestamps: keep original but trim a tiny bit
            result.append({"start": seg["start"], "end": seg["end"],
                           "text": seg["text"], "language": seg["language"]})
            continue

        # Group words, splitting at pauses (>0.8s gap)
        groups = []
        cur = [words[0]]
        for w in words[1:]:
            if w.start - cur[-1].end > 0.8:
                groups.append(cur)
                cur = [w]
            else:
                cur.append(w)
        groups.append(cur)

        for group in groups:
            if not group:
                continue
            first_w, last_w = group[0], group[-1]
            text = " ".join(w.word.strip() for w in group)
            result.append({
                "start": first_w.start,
                "end": last_w.end,
                "text": text,
                "language": seg["language"],
            })

    return result


# ── Profanity filter ─────────────────────────────────────────────────────

_CENSOR = {
    "joder", "jodido", "jodida", "jodidos", "jodidas",
    "puta", "puto", "putas", "putos",
    "mierda", "mierdas",
    "coño", "coño", "coños",
    "hostia", "hostias",
    "gilipollas", "gilipoya",
    "cabrón", "cabrona", "cabrones", "cabronas",
    "pendejo", "pendejos", "pendeja", "pendejas",
    "culo", "culito", "culos", "culata",
    "polla", "poyas", "polla", "poyas",
    "verga", "vergas",
    "chinga", "chingar", "chingado", "chingada",
    "pinche", "pinches",
    "malparido", "malparida", "malparidos", "malparidas",
    "marica", "maricón", "maricon", "maricas", "maricones",
    "zorra", "zorro", "zorras", "zorros",
    "bastardo", "bastarda", "bastardos", "bastardas",
}

def _censor_word(w: str) -> str:
    """Replace a profanity word with asterisks, keeping first letter."""
    low = w.lower().strip(",.!?¿¡;:")
    if low in _CENSOR:
        return w[0] + "*" * (len(w) - 1) if len(w) > 1 else "*"
    return w

def _censor_text(text: str) -> str:
    """Censor profanity in a subtitle line."""
    return " ".join(_censor_word(w) for w in text.split())


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
    font_size: int = 60,
    margin_bottom: int = 140,
    style: dict | None = None,
) -> str:
    """Convert subtitle entries to ASS format with TikTok styling.

    Args:
        entries: list of {start, end, text} from transcribe()
        video_w, video_h: output video dimensions
        font_size: font size in points
        margin_bottom: distance from bottom edge
        style: optional overrides: {
            word_level: bool  — word-by-word karaoke
            bg: bool          — show background box (default True)
            bg_color: str     — color name for background
            font_color: str   — color name for text
        }
    """
    cfg = {
        "word_level": False,
        "bg": True,
        "bg_color": None,
        "font_color": None,
        "outline": 0,         # text border/stroke width (0 = none)
        "outline_color": None,  # border color (default: black)
    }
    if style:
        cfg.update(style)

    # Resolve colors
    text_color = _resolve_color(cfg["font_color"]) or (255, 255, 255)
    bg_color = _resolve_color(cfg["bg_color"]) or (0, 0, 0)
    outline_color = _resolve_color(cfg["outline_color"]) or (0, 0, 0)
    # ASS format: &HAABBGGRR (alpha-blue-green-red in hex)
    tc = f"&H00{text_color[2]:02X}{text_color[1]:02X}{text_color[0]:02X}"
    bc = f"&H80{bg_color[2]:02X}{bg_color[1]:02X}{bg_color[0]:02X}"
    oc = f"&H00{outline_color[2]:02X}{outline_color[1]:02X}{outline_color[0]:02X}"
    outline = cfg.get("outline", 0)

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_w}
PlayResY: {video_h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{font_size},{tc},{oc},{bc},-1,0,3,{outline},0,2,100,100,{margin_bottom},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    for entry in entries:
        start = _fmt_ass_time(entry["start"])
        end = _fmt_ass_time(entry["end"])
        text = _censor_text(entry["text"].strip().upper())
        if cfg["word_level"]:
            # Each word gets its own entry
            words = text.split()
            if not words:
                continue
            wdur = (entry["end"] - entry["start"]) / len(words)
            for i, word in enumerate(words):
                ws = _fmt_ass_time(entry["start"] + i * wdur)
                we = _fmt_ass_time(entry["start"] + (i + 1) * wdur)
                ass += f"Dialogue: 0,{ws},{we},Default,,0,0,0,,{word}\n"
        else:
            ass += f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n"

    return ass


def _resolve_color(name: str | None) -> tuple | None:
    """Convert color name or hex to RGB. Returns None if unknown."""
    if not name:
        return None
    # Hex: #RRGGBB or RRGGBB
    if name.startswith("#"):
        name = name[1:]
    if len(name) == 6 and all(c in "0123456789ABCDEFabcdef" for c in name):
        return (int(name[0:2], 16), int(name[2:4], 16), int(name[4:6], 16))
    named = {
        # Neutros
        "white": (255,255,255), "blanco": (255,255,255),
        "black": (0,0,0), "negro": (0,0,0),
        "gray": (128,128,128), "gris": (128,128,128),
        "silver": (192,192,192), "plateado": (192,192,192),
        # Primarios
        "red": (255,0,0), "rojo": (255,0,0),
        "green": (0,255,0), "verde": (0,255,0),
        "blue": (0,0,255), "azul": (0,0,255),
        # Secundarios
        "yellow": (255,255,0), "amarillo": (255,255,0),
        "cyan": (0,255,255), "cian": (0,255,255),
        "magenta": (255,0,255), "magenta": (255,0,255),
        # Comunes
        "orange": (255,165,0), "naranja": (255,165,0),
        "purple": (128,0,128), "morado": (128,0,128), "púrpura": (128,0,128),
        "pink": (255,192,203), "rosa": (255,192,203),
        "brown": (139,69,19), "marron": (139,69,19), "marrón": (139,69,19),
        "lime": (50,205,50), "lima": (50,205,50),
        "navy": (0,0,128), "azul marino": (0,0,128),
        "teal": (0,128,128), "turquesa": (0,128,128),
        "coral": (255,127,80), "coral": (255,127,80),
        "gold": (255,215,0), "dorado": (255,215,0), "oro": (255,215,0),
        "violet": (238,130,238), "violeta": (238,130,238),
    }
    return named.get(name.lower())


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

    # On Windows, ffmpeg chokes on drive-letter paths in filter args
    # (C:\... gets double-escaped). Use relative paths instead.
    video_path_rel = os.path.relpath(video_path)
    ass_path_rel = os.path.relpath(ass_path)

    in_file = ffmpeg.input(video_path_rel)
    scaled = ffmpeg.filter(in_file, "scale", 1080, 1920)
    subbed = ffmpeg.filter(scaled, "subtitles", ass_path_rel)

    args = {"vcodec": encoder, "acodec": "aac", "r": fps}
    if encoder == "h264_nvenc":
        args["preset"] = "p4"

    try:
        # Include audio: scale+subtitles filters are video-only,
        # so we must map the audio stream from the input explicitly.
        stream = ffmpeg.output(subbed, in_file.audio, output_path, **args)
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
