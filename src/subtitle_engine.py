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

def _segments_to_entries(segments, lang_code, word_level):
    """Convert Whisper segments to subtitle entry dicts."""
    entries = []
    for seg in segments:
        txt = (seg.text or "").strip()
        if not txt:
            continue
        logp = getattr(seg, "avg_logprob", -99) or -99
        if word_level and seg.words:
            for w in seg.words:
                wt = (w.word or "").strip()
                if wt:
                    entries.append({"start": w.start, "end": w.end, "text": wt,
                                    "language": lang_code, "level": "word",
                                    "avg_logprob": logp})
        else:
            words = txt.split()
            n = len(words)
            seg_words = [w for w in (seg.words or []) if (w.word or "").strip()]
            if n <= 5 and len(txt) <= MAX_CHARS_PER_LINE:
                # Short phrase: use Whisper segment times, trimmed to real
                # speech (kills trailing silence Whisper often appends).
                start, end = seg.start, seg.end
                if seg_words:
                    start, end = seg_words[0].start, seg_words[-1].end
                entries.append({"start": start, "end": end, "text": txt,
                                "language": lang_code, "level": "phrase",
                                "avg_logprob": logp})
            elif len(seg_words) >= n:
                # Long phrase: chunk by REAL word timestamps instead of
                # dividing the segment into equal time slices. Speech rate is
                # not uniform, so proportional chunking drifts from audio.
                # Chunks are also capped at MAX_CHARS_PER_LINE so the caption
                # never exceeds a 6.7" phone screen width.
                chunk_size = max(3, n // max(1, int((seg.end - seg.start) * 2.0)))
                chunk_gap = 0.75  # new chunk if silence gap between words
                chunk = []
                prev_end = None
                chunk_chars = 0
                for w in seg_words:
                    wlen = len(w.word.strip())
                    # Cut before this word if the chunk is full (words, chars
                    # or silence gap)
                    if chunk and (
                        len(chunk) >= chunk_size
                        or chunk_chars + wlen + 1 > MAX_CHARS_PER_LINE
                        or (prev_end is not None and (w.start - prev_end) > chunk_gap)
                    ):
                        entries.append({"start": chunk[0].start, "end": chunk[-1].end,
                                        "text": " ".join(x.word.strip() for x in chunk),
                                        "language": lang_code, "level": "phrase",
                                        "avg_logprob": logp})
                        chunk = []
                        chunk_chars = 0
                    chunk.append(w)
                    chunk_chars += wlen + (1 if chunk_chars else 0)
                    prev_end = w.end
                if chunk:
                    entries.append({"start": chunk[0].start, "end": chunk[-1].end,
                                    "text": " ".join(x.word.strip() for x in chunk),
                                    "language": lang_code, "level": "phrase",
                                    "avg_logprob": logp})
            else:
                # Fallback (no word timestamps available): proportional split
                # with char cap.
                chunk_size = max(3, n // max(1, int((seg.end - seg.start) * 2.0)))
                chunks = []
                i = 0
                while i < n:
                    chunk = []
                    clen = 0
                    while i < n and len(chunk) < chunk_size and clen + len(words[i]) + (1 if clen else 0) <= MAX_CHARS_PER_LINE:
                        chunk.append(words[i])
                        clen += len(words[i]) + (1 if clen else 0)
                        i += 1
                    if chunk:
                        chunks.append(" ".join(chunk))
                dur = seg.end - seg.start
                chunk_dur = dur / len(chunks) if chunks else 0
                for ci, chunk in enumerate(chunks):
                    entries.append({"start": seg.start + ci * chunk_dur,
                                    "end": seg.start + (ci + 1) * chunk_dur,
                                    "text": chunk, "language": lang_code, "level": "phrase",
                                    "avg_logprob": logp})
    return entries


def transcribe_segment(
    audio_path: str,
    word_level: bool = False,
    languages: list[str] | None = None,
) -> list[dict]:
    """Transcribe audio with faster-whisper (GPU).
    When languages is None/empty, transcribes ONCE with auto language detection.
    When multiple languages requested, transcribes once per language,
    keeps entries whose text actually matches the expected language (langdetect)."""
    from faster_whisper import WhisperModel
    from config import get_whisper_model, get_whisper_device, get_whisper_compute_type
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0

    model = WhisperModel(
        get_whisper_model(), device=get_whisper_device(),
        compute_type=get_whisper_compute_type(),
    )

    if not languages:
        # Auto-detect: single transcription pass. Whisper detects the audio
        # language itself. Per-entry langdetect tags each phrase with its
        # actual language → English phrases render white, Spanish yellow.
        # One pass = no duplicated subtitles.
        segments, info = model.transcribe(audio_path, vad_filter=True,
                                           word_timestamps=True)
        fallback_lang = getattr(info, "language", None) or "es"
        entries = _segments_to_entries(segments, "auto", word_level)
        for entry in entries:
            try:
                entry["language"] = detect(entry["text"])
            except Exception:
                entry["language"] = fallback_lang
        entries.sort(key=lambda e: e["start"])
        entries = _dedup_entries(entries)
        logger.info(f"Subtitle engine: {len(entries)} entries (auto-detect, per-entry lang)")
        return entries

    # Explicit single language: transcribe once in that language
    if len(languages) == 1:
        lang = languages[0]
        segments, info = model.transcribe(audio_path, vad_filter=True,
                                           language=lang, word_timestamps=True)
        entries = _segments_to_entries(segments, lang, word_level)
        entries.sort(key=lambda e: e["start"])
        entries = _dedup_entries(entries)
        return entries

    # Explicit bilingual: transcribe once per language, filter by langdetect
    # (Whisper sometimes outputs English words even with language="es",
    #  so we check that the text actually matches the expected language)
    all_entries = []
    for lang_code in languages:
        try:
            segments, _ = model.transcribe(audio_path, vad_filter=True,
                                            language=lang_code, word_timestamps=True)
            entries = _segments_to_entries(segments, lang_code, word_level)
            filtered = _filter_by_language(entries, lang_code)
            all_entries.extend(filtered)
            logger.info(f"  {lang_code}: {len(entries)} raw -> {len(filtered)} kept")
        except Exception as e:
            logger.warning(f"  {lang_code} transcription failed: {e}")

    all_entries.sort(key=lambda e: e["start"])
    all_entries = _dedup_entries(all_entries)
    logger.info(f"Subtitle engine: {len(all_entries)} total entries, bilingual")
    return all_entries


def _filter_by_language(entries, expected_lang):
    """Keep entries whose text is detected as the expected language.
    Filters out translations: e.g., Spanish text in an English transcription pass."""
    from langdetect import detect
    result = []
    for e in entries:
        txt = e["text"]
        try:
            detected = detect(txt)
        except Exception:
            result.append(e)
            continue
        is_english = (detected == "en")
        wants_english = (expected_lang == "en")
        if is_english == wants_english:
            result.append(e)
    return result


def _dedup_entries(entries):
    """Remove duplicate (start, text) pairs so no caption is burned twice.
    Rounds start to 2 decimals: es/en passes over the same audio produce
    nearly-identical (but not float-equal) start times."""
    seen = set()
    deduped = []
    for e in entries:
        key = (round(e["start"], 2), e["text"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    return deduped


# ── Rendering ────────────────────────────────────────────────────────────────

# Default style configuration
# UNIFIED design: white text on semi-transparent black box. One look across
# all clips — no per-language colors, no bg toggle.
DEFAULT_STYLE = {
    "bg": True,
    "bg_color": (0, 0, 0),
    "bg_opacity": 0.7,
    "bg_padding": (20, 10),
    "default_color": (255, 255, 255),
    "stroke_color": (0, 0, 0),
    "stroke_width": 2,             # subtle edge on light video areas
    "position": 0.82,              # bottom band, above TikTok UI (~84% down)
    "multi_lang_offset": 0.04,
}

# Max characters per subtitle line. Calculated for a 6.7" phone screen:
# render at 540px wide (half-res, later upscaled ×2 to 1080), useful width
# ~94% = ~507px, Arial Bold 35px ≈ 20px/char → ~24 chars fit comfortably.
MAX_CHARS_PER_LINE = 24

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
        color = cfg["default_color"]

        font_path = _find_font()
        font_size = _font_size(clip.w)

        # Vertical position: offset by language, clamp to prevent overflow
        lang_idx = LANG_ORDER.index(lang) if lang in LANG_ORDER else 0
        pos_y = int(clip.h * (cfg["position"] - lang_idx * cfg["multi_lang_offset"]))

        # Unified design: white text on black box. Use label (exact-size box)
        # normally; if the rendered text is wider than the phone screen,
        # fall back to caption method so it wraps instead of being cut.
        max_w = int(clip.w * 0.94)
        txt = TextClip(
            text=text.upper(), font=font_path, font_size=font_size,
            color=color, stroke_color=cfg["stroke_color"],
            stroke_width=cfg["stroke_width"],
            method="label",
        )
        if txt.w > max_w:
            txt = TextClip(
                text=text.upper(), font=font_path, font_size=font_size,
                color=color, stroke_color=cfg["stroke_color"],
                stroke_width=cfg["stroke_width"],
                method="caption", size=(max_w, None), text_align="center",
            )
        dur = max(end - start, 0.2)

        if cfg["bg"]:
            w, h = txt.w + cfg["bg_padding"][0], txt.h + cfg["bg_padding"][1]
            bg = ColorClip(size=(w, h), color=cfg["bg_color"])
            bg = bg.with_opacity(cfg["bg_opacity"])
            frame = CompositeVideoClip([
                bg.with_position(("center", "center")),
                txt.with_position(("center", "center")),
            ]).with_duration(dur)
        else:
            frame = txt.with_duration(dur)

        # Center vertically at pos_y, clamp to keep fully inside frame
        top = pos_y - frame.h // 2
        top = max(0, min(top, clip.h - frame.h))
        frame = frame.with_position(("center", top))
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
