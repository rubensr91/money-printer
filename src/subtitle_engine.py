"""
Subtitle Engine — Whisper GPU transcription + burned-in captions.

Supports:
- Multi-language detection (ES/EN auto-tagged)
- Word-level (karaoke) vs phrase-level
- Background bounding box (semi-transparent)
- Language-specific colors (ES=yellow, EN=white)
- TikTok-style positioning
"""

import os
import uuid
import logging

logger = logging.getLogger(__name__)


# ── Transcription ───────────────────────────────────────────────────────────

def _segments_to_entries(segments, default_lang, word_level):
    """Convert Whisper segments to subtitle entry dicts with language tag."""
    entries = []
    for seg in segments:
        txt = (seg.text or "").strip()
        if not txt:
            continue
        if word_level and seg.words:
            for w in seg.words:
                wt = (w.word or "").strip()
                if wt:
                    entries.append({"start": w.start, "end": w.end, "text": wt,
                                    "language": default_lang, "level": "word"})
        else:
            words = txt.split()
            n = len(words)
            if n <= 5:
                entries.append({"start": seg.start, "end": seg.end, "text": txt,
                                "language": default_lang, "level": "phrase"})
            else:
                chunk_size = max(3, n // max(1, int((seg.end - seg.start) * 2.0)))
                chunks = []
                i = 0
                while i < n:
                    chunks.append(" ".join(words[i:i + chunk_size]))
                    i += chunk_size
                dur = seg.end - seg.start
                chunk_dur = dur / len(chunks) if chunks else 0
                for ci, chunk in enumerate(chunks):
                    entries.append({"start": seg.start + ci * chunk_dur,
                                    "end": seg.start + (ci + 1) * chunk_dur,
                                    "text": chunk, "language": default_lang, "level": "phrase"})
    return entries


def _transcribe_multilang(audio_path, model, languages, word_level):
    """Transcribe once per language, merge entries sorted by time.
    Each language gets its own color/position in the render step."""
    all_entries = []
    for lang_code in languages:
        try:
            segments, info = model.transcribe(audio_path, vad_filter=True,
                                               language=lang_code, word_timestamps=word_level)
            entries = _segments_to_entries(segments, lang_code, word_level)
            all_entries.extend(entries)
            logger.info(f"  Multi-lang: {len(entries)} entries for {lang_code}")
        except Exception as e:
            logger.warning(f"  Multi-lang {lang_code} failed: {e}")

    all_entries.sort(key=lambda e: e["start"])
    logger.info(f"Subtitle engine: {len(all_entries)} total entries, multi-lang={languages}")
    return all_entries


def transcribe_segment(
    audio_path: str,
    word_level: bool = False,
    languages: list[str] | None = None,
) -> list[dict]:
    """Transcribe audio with faster-whisper (GPU).

    Args:
        audio_path: WAV file (16kHz mono)
        word_level: If True, one entry per word with exact timestamps.
        languages: List of language codes to accept (e.g., ["es","en"]).
                   If None, auto-detect. Multi-lang enables multilingual mode.

    Returns:
        List of dicts: {start, end, text, language, level}
        level = "word" or "phrase"
    """
    from faster_whisper import WhisperModel
    from config import get_whisper_model, get_whisper_device, get_whisper_compute_type

    model = WhisperModel(
        get_whisper_model(),
        device=get_whisper_device(),
        compute_type=get_whisper_compute_type(),
    )

    # Transcribe
    # Multiple languages requested → transcribe once per language, merge
    if languages and len(languages) > 1:
        return _transcribe_multilang(audio_path, model, languages, word_level)
    else:
        lang = languages[0] if languages else None
        segments, info = model.transcribe(audio_path, vad_filter=True,
                                           language=lang, word_timestamps=word_level)
        return _segments_to_entries(segments, info.language, word_level)


# ── Rendering ────────────────────────────────────────────────────────────────

# Default style configuration
DEFAULT_STYLE = {
    "bg": False,
    "bg_color": (0, 0, 0),
    "bg_opacity": 0.7,
    "bg_padding": (20, 10),
    "lang_colors": {
        "es": (255, 213, 0),       # Spanish: yellow
        "en": (255, 255, 255),     # English: white
    },
    "default_color": (255, 255, 255),
    "stroke_color": (0, 0, 0),
    "stroke_width": 4,             # TikTok standard: 4-6px black stroke
    "position": 0.62,              # lower-middle third, above TikTok bottom UI
    "multi_lang_offset": 0.06,
}

# Language display order (top to bottom)
LANG_ORDER = ["en", "es"]


def render_subtitles(
    clip,
    entries: list[dict],
    style: dict | None = None,
) -> "CompositeVideoClip":
    """Burn subtitles onto a MoviePy clip.

    Args:
        clip: MoviePy VideoFileClip or CompositeVideoClip
        entries: List from transcribe_segment()
        style: Override keys from DEFAULT_STYLE

    Returns:
        CompositeVideoClip with subtitles composited on top.
    """
    from moviepy import TextClip, ColorClip, CompositeVideoClip

    cfg = dict(DEFAULT_STYLE)
    if style:
        cfg.update(style)

    sub_clips = []

    for entry in entries:
        start = entry["start"]
        end = entry["end"]
        text = entry["text"]
        lang = entry.get("language", "en")
        color = cfg["lang_colors"].get(lang, cfg["default_color"])

        font_path = _find_font()
        font_size = _font_size(clip.w)

        # Vertical position: offset by language to avoid overlap
        lang_idx = LANG_ORDER.index(lang) if lang in LANG_ORDER else 0
        pos_y = int(clip.h * (cfg["position"] - lang_idx * cfg["multi_lang_offset"]))

        if cfg["bg"]:
            # Create text + background box
            txt = TextClip(
                text=text.upper(), font=font_path, font_size=font_size,
                color=color, stroke_color=cfg["stroke_color"],
                stroke_width=cfg["stroke_width"],
                method="label",
            )
            w, h = txt.w + cfg["bg_padding"][0], txt.h + cfg["bg_padding"][1]
            bg = ColorClip(size=(w, h), color=cfg["bg_color"])
            bg = bg.with_opacity(cfg["bg_opacity"])
            frame = CompositeVideoClip([
                bg.with_position(("center", "center")),
                txt.with_position(("center", "center")),
            ]).with_duration(max(end - start, 0.2))
            frame = frame.with_position(("center", pos_y - h // 2))
            frame = frame.with_start(start)
        else:
            # Text only with stroke
            frame = TextClip(
                text=text.upper(), font=font_path, font_size=font_size,
                color=color, stroke_color=cfg["stroke_color"],
                stroke_width=cfg["stroke_width"],
                method="caption",
                size=(int(clip.w * 0.9), None),
                text_align="center",
            ).with_duration(max(end - start, 0.2))
            frame = frame.with_position(("center", pos_y))
            frame = frame.with_start(start)

        sub_clips.append(frame)

    return CompositeVideoClip([clip, *sub_clips])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_font():
    """Find best font. Bold preferred (31% more readable on mobile per TikTok research)."""
    from config import ROOT_DIR
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",         # Arial Bold (best)
        "C:/Windows/Fonts/arial.ttf",           # Arial Regular
        os.path.join(ROOT_DIR, "fonts", "Arial.ttf"),
        os.path.join(ROOT_DIR, "fonts", "bold_font.ttf"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return "Arial"


def _font_size(video_width: int) -> int:
    """Dynamic font size. Base: 70px for 1080px width (TikTok optimal 60-75px range)."""
    return int(video_width * 0.065)


# ── Convenience ──────────────────────────────────────────────────────────────

def extract_audio_segment(
    video_path: str,
    clip_start: float,
    clip_end: float,
    output_dir: str,
) -> str:
    """Extract audio from a video segment as 16kHz mono WAV (for Whisper).
    Returns path to WAV file. Caller should clean up."""
    import subprocess
    from tiktok_clips import _find_local_ffmpeg

    tmp = os.path.join(output_dir, f"_subs_{uuid.uuid4().hex[:8]}.wav")
    ffmpeg = _find_local_ffmpeg()
    subprocess.run([
        ffmpeg, "-y", "-i", video_path,
        "-ss", str(clip_start), "-to", str(clip_end),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", tmp,
    ], check=True, capture_output=True)
    return tmp
