"""
TikTok Clip Extractor - MoneyPrinterV2
Downloads YouTube video, extracts auto-captions (no Whisper),
uses DeepSeek to find viral moments, renders in panoramic format
(original 16:9 centered on 9:16 with pixelated video background).
No subtitles rendered, no face tracking.
"""
import os, sys, re, json, uuid, subprocess, tempfile, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Prepend project-local ffmpeg (NVENC-compatible build) to PATH
# This ffmpeg 6.1.1 works with older NVIDIA drivers (546.x) where
# the system ffmpeg 8.x requires driver 551.76+.
_LOCAL_FFMPEG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".mp", "tools")
_FFMPEG_BIN = None
if os.path.isdir(_LOCAL_FFMPEG_DIR):
    _candidate = os.path.join(_LOCAL_FFMPEG_DIR, "ffmpeg.exe")
    if os.path.exists(_candidate):
        _FFMPEG_BIN = _candidate
        os.environ["PATH"] = _LOCAL_FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")
        os.environ["FFMPEG_BINARY"] = _candidate

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

from moviepy import VideoFileClip, CompositeVideoClip, afx
if _FFMPEG_BIN:
    import moviepy.config as _mpcfg
    _mpcfg.FFMPEG_BINARY = _FFMPEG_BIN

# Render resolution: process frames at HALF res (540x960 = 4x fewer pixels per
# frame in the Python compositing loop), then ffmpeg upscales to 1080x1920
# during encode (cheap swscale). Pixelated background hides the upscale loss.
# Huge speedup: Python frame loop was the bottleneck, not the encoder.
_RENDER_W, _RENDER_H = 540, 960
_FINAL_W, _FINAL_H = 1080, 1920
from config import ROOT_DIR, get_threads, assert_folder_structure, get_deepseek_model
from llm_provider import generate_text, select_model, get_active_model

_PROJECT_DIR = ROOT_DIR

assert_folder_structure()
select_model(get_deepseek_model())


def _csafe(msg, tag, color):
    """Print safely, replacing unprintable chars on Windows cp1252."""
    try:
        print(f"\033[{color}m[{tag}] {msg}\033[0m")
    except UnicodeEncodeError:
        print(f"\033[{color}m[{tag}] {msg.encode('ascii', 'replace').decode()}\033[0m")


def info(msg):  _csafe(msg, "INFO", "94")
def ok(msg):    _csafe(msg, "OK", "92")
def warn(msg):  _csafe(msg, "WARN", "93")
def err(msg):   _csafe(msg, "ERROR", "91")


# ── YouTube download + captions ──────────────────────────────────────────

def _extract_video_id(url):
    """Extract stable video ID from URL. Same URL = same ID always.
    YouTube: 11-char video ID from path. TikTok: numeric video ID.
    Fallback: MD5 hash of the clean URL (stable across repeated downloads)."""
    # YouTube patterns
    for pat in (r"v=([\w-]{11})", r"youtu\.be/([\w-]{11})", r"/(?:embed|shorts|v)/([\w-]{11})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    # TikTok patterns: @user/video/123456789 or vm.tiktok.com/short
    m = re.search(r'tiktok\.com/(?:@[\w.-]+/video/|.*?/)(\d+)', url)
    if m:
        return f"tt_{m.group(1)}"
    # Instagram: reel ID from path
    m = re.search(r'instagram\.com/(?:reel|p)/([\w-]+)', url)
    if m:
        return f"ig_{m.group(1)}"
    # Fallback: MD5 hash of the URL (stable, same URL → same file)
    return hashlib.md5(url.encode()).hexdigest()[:12]


def is_playlist_url(url):
    """True if URL points to a YouTube playlist."""
    return "playlist" in url.lower() or "&list=" in url


def detect_source_type(url):
    """Return platform label: 'youtube', 'twitch', 'tiktok', 'direct', 'instagram', 'unknown'."""
    low = url.lower()
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    if "twitch.tv" in low:
        return "twitch"
    if "tiktok.com" in low:
        return "tiktok"
    if "instagram.com" in low:
        return "instagram"
    if low.endswith(".mp4") or low.endswith(".webm") or low.endswith(".mov"):
        return "direct"
    return "unknown"


def download_media(url, output_dir):
    """Platform-aware download. Returns (video_path, caption_path).
    YouTube -> with captions. Twitch/Instagram -> video via yt-dlp (no captions).
    Direct .mp4 URL -> streamed download (no captions)."""
    stype = detect_source_type(url)

    if stype == "youtube":
        return download_youtube(url, output_dir)

    vid = _extract_video_id(url) if stype != "direct" else str(uuid.uuid4())[:8]
    video_path = os.path.join(output_dir, f"{vid}.mp4")

    if os.path.exists(video_path):
        ok(f"Cached: {vid}.mp4")
        return video_path, None

    if stype == "direct":
        info(f"Downloading direct: {url}")
        import urllib.request
        try:
            urllib.request.urlretrieve(url, video_path)
            return video_path, None
        except Exception as e:
            err(f"Direct download failed: {e}")
            raise RuntimeError(f"Direct download failed: {e}")

    # Twitch / Instagram / TikTok via yt-dlp
    # TikTok serves separate video + audio streams: prefer merge,
    # fall back to best combined, then any best.
    info(f"Downloading ({stype}): {url}")
    template = os.path.join(output_dir, f"{vid}.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "-o", template, "--merge-output-format", "mp4", "--no-playlist",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err(f"yt-dlp ({stype}) failed: {result.stderr[:300]}")
        raise RuntimeError(f"Download failed: {result.stderr[:200]}")
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")
    ok(f"Downloaded ({stype}): {vid}.mp4")
    return video_path, None


def extract_playlist_urls(playlist_url, limit=20):
    """Expand a YouTube playlist into individual video URLs (max `limit`)."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--flat-playlist", "--print", "url", "--no-warnings",
        "--playlist-items", f"1:{limit}",
        playlist_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err(f"yt-dlp playlist failed: {result.stderr[:300]}")
        return []
    urls = [line.strip() for line in result.stdout.splitlines()
            if "youtube.com" in line or "youtu.be" in line]
    return urls


def download_youtube(url, output_dir):
    """Download video and auto-generated captions from YouTube.
    Caches by video ID — skips download if already present."""
    vid = _extract_video_id(url)
    video_path = os.path.join(output_dir, f"{vid}.mp4")
    caption_path = None
    for ext in (".es.vtt", ".vtt"):
        maybe = os.path.join(output_dir, f"{vid}{ext}")
        if os.path.exists(maybe):
            caption_path = maybe
            break

    if os.path.exists(video_path):
        if caption_path:
            ok(f"Cached: {vid}.mp4 + captions")
        else:
            ok(f"Cached: {vid}.mp4 (no captions)")
        return video_path, caption_path

    info(f"Downloading: {url}")
    template = os.path.join(output_dir, f"{vid}.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
        "-o", template, "--merge-output-format", "mp4", "--no-playlist",
        "--write-auto-subs", "--sub-lang", "es",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err(f"yt-dlp failed: {result.stderr}")
        raise RuntimeError(result.stderr)
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")
    # Find captions file
    for ext in (".es.vtt", ".vtt"):
        maybe = os.path.join(output_dir, f"{vid}{ext}")
        if os.path.exists(maybe):
            caption_path = maybe
            break
    if caption_path:
        ok(f"Downloaded + captions -> {vid}")
    else:
        ok(f"Downloaded (no auto-captions) -> {vid}")
    return video_path, caption_path


def parse_vtt(vtt_path):
    """Parse VTT auto-captions into list of {start, end, text}."""
    if not vtt_path or not os.path.exists(vtt_path):
        return []

    with open(vtt_path, "r", encoding="utf-8") as f:
        raw = f.read()

    def _pts(s):
        p = s.split(":")
        return float(p[0])*3600 + float(p[1])*60 + float(p[2].replace(",", "."))

    blocks = raw.strip().split("\n\n")
    segments = []
    for block in blocks:
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            m = re.match(r"(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})", line)
            if m:
                ts, te = _pts(m.group(1)), _pts(m.group(2))
                if te - ts < 0.5:  # skip trigger markers
                    break
                text = " ".join(lines[i+1:])
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\s+", " ", text).strip()
                if text and text != "[Música]":
                    segments.append({"start": ts, "end": te, "text": text})
                break

    # Dedup: remove consecutive entries that are subsets of accumulated text
    final = []
    prev = ""
    for s in segments:
        if s["text"] not in prev and not prev.endswith(s["text"]):
            final.append(s)
            prev = s["text"]
    return final


# ── DeepSeek clip selection ──────────────────────────────────────────────

def find_best_moments(segments, video_duration, min_clip=5, max_clip=0, num_clips=3, instructions=None):
    """Use DeepSeek to find most viral moments from captions.
    Then snap boundaries to nearest segment to avoid mid-sentence cuts.
    `instructions` (optional) = user-provided directives that take priority.
    max_clip=0 (default) = NO upper limit; user instructions decide chunking."""
    info("Analyzing captions with DeepSeek to find best moments...")
    if instructions:
        warn(f"User instructions: {instructions[:200]}")

    transcript = ""
    for seg in segments:
        transcript += f"[{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}\n"

    # max_clip=0 means unlimited; cap at video length only for the prompt text
    eff_max = video_duration if (not max_clip or max_clip <= 0) else min(max_clip, video_duration)

    user_rules = f"""
- El video dura {video_duration:.0f}s. Busca {num_clips} clips de al menos {min_clip}s.
- {'El clip puede durar hasta ' + str(int(eff_max)) + 's (sin limite fijo, usa tu criterio segun el contenido).' if (not max_clip or max_clip <= 0) else 'Cada clip entre ' + str(min_clip) + 's y ' + str(int(eff_max)) + 's.'}
- Mas vale 1 clip bueno de 30s que 3 malos de 15s.
- NO cortes frases a medias NUNCA. Mira los timestamps de la transcripcion, y ajusta start/end para que coincidan EXACTAMENTE con el inicio/fin de una frase completa.
- Prioriza clips de >{min_clip}s. Si no hay suficientes momentos largos, reduce el numero de clips.
- Los clips deben ser auto-contenidos. El que los vea entiende el contexto.
"""
    if instructions:
        user_rules += f"""
INSTRUCCIONES PRIMORDIALES DEL USUARIO (MAXIMA PRIORIDAD SOBRE TODO LO DEMAS, CUMPLELAS SI O SI):
{instructions}
"""

    prompt = f"""Analiza esta transcripcion de YouTube y dime cuales son los momentos mas virales.

REGLAS CRITICAS:
{user_rules}
ADEMAS de los clips, genera:
- "description": descripcion viral para TikTok de maximo 150 caracteres, enganchosa, con emojis si procede
- "tags": array de 5-10 hashtags relevantes SIN el simbolo # (ej: "streamer", "humor")

DEVUELVE SOLO JSON:
{{
  "description": "descripcion viral de max 150 chars",
  "tags": ["tag1", "tag2", "tag3"],
  "clips": [
    {{"start": 12.0, "end": 38.5, "reason": "explica por que es viral"}}
  ]
}}

Transcripcion con timestamps:
{transcript[:40000]}"""

    def _parse_response(response):
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
                    err(f"DeepSeek returned unparseable JSON: {response[:300]}")
                    return {}, []
            else:
                err(f"DeepSeek returned no JSON: {response[:300]}")
                return {}, []
        return data, data.get("clips", [])

    def _validate(clips):
        seg_boundaries = set()
        for s in segments:
            seg_boundaries.add(round(s["start"], 1))
            seg_boundaries.add(round(s["end"], 1))
        boundaries = sorted(seg_boundaries)

        def snap_to_boundary(t):
            best, best_dist = t, 999
            for b in boundaries:
                d = abs(b - t)
                if d < best_dist:
                    best, best_dist = b, d
            return best

        validated = []
        rejected = []
        for c in clips:
            start = float(c.get("start", 0))
            end = float(c.get("end", 0))
            start = snap_to_boundary(start)
            end = snap_to_boundary(end)
            dur = end - start
            if dur < 8 or dur > video_duration:
                rejected.append(f"{c.get('start')}-{c.get('end')} dur={dur:.1f}s out of range")
                continue
            if start < 0:
                start = 0
            if end > video_duration:
                end = video_duration
            overlaps = False
            for v in validated:
                if not (end <= v["start"] or start >= v["end"]):
                    overlaps = True
                    break
            if overlaps:
                rejected.append(f"{c.get('start')}-{c.get('end')} overlaps validated")
                continue
            validated.append({"start": start, "end": end, "reason": c.get("reason", "")})
        if rejected:
            warn(f"Rejected {len(rejected)} clips: {'; '.join(rejected[:5])}")
        return validated

    # Attempt 1: full prompt
    response = generate_text(prompt)
    meta, clips = _parse_response(response)
    validated = _validate(clips)

    # Attempt 2: if 0 valid, retry with a simpler, more forceful prompt
    if not validated:
        warn("DeepSeek returned 0 valid clips, retrying with stricter prompt...")
        retry_prompt = f"""Transcripcion de un video de {video_duration:.0f}s con timestamps.
Devuelve SOLO JSON con {num_clips} clips virales. CADA clip:
- start y end deben ser timestamps EXACTOS de la transcripcion (frases completas, sin cortes)
- duracion de al menos {min_clip}s{' (sin limite maximo)' if (not max_clip or max_clip <= 0) else f' entre {min_clip}s y {int(eff_max)}s'}
- usa los timestamps [X.Xs-Y.Ys] de la transcripcion como referencia

ADEMAS incluye:
- "description": descripcion viral de max 150 chars
- "tags": array de 5-10 hashtags SIN el simbolo #

{('INSTRUCCIONES DEL USUARIO (CUMPLELAS): ' + instructions) if instructions else ''}

JSON:
{{"description": "...", "tags": ["..."], "clips": [{{"start": 0.0, "end": 0.0, "reason": ""}}]}}

Transcripcion:
{transcript[:40000]}"""
        response2 = generate_text(retry_prompt)
        meta2, clips2 = _parse_response(response2)
        validated = _validate(clips2)
        if validated:
            meta = meta2

    description = str(meta.get("description", "")).strip()[:150]
    tags = [str(t).strip("#").strip() for t in meta.get("tags", []) if str(t).strip()]
    tags = tags[:10]

    ok(f"DeepSeek found {len(validated)} viral moments")
    for i, v in enumerate(validated):
        print(f"  [{i+1}] {v['start']:.0f}s-{v['end']:.0f}s ({v['end']-v['start']:.0f}s) | {v['reason'][:80]}")
    if description:
        ok(f"Description: {description[:80]}...")
    if tags:
        ok(f"Tags: {', '.join(tags[:5])}...")
    return validated, description, tags


def _split_timebased(duration, min_clip=5, max_clip=0, num_clips=4):
    """Fallback: split video duration into N clips evenly.
    max_clip=0 (default) = no upper limit (whole video if num_clips=1)."""
    if max_clip and max_clip > 0 and max_clip < duration:
        pass
    else:
        max_clip = duration  # no cap: the only bound is the video itself
    if min_clip > max_clip:
        min_clip = max_clip
    chunk = min(max_clip, duration / num_clips)
    if chunk < min_clip:
        chunk = min_clip
    moments = []
    start = 0
    while start < duration and len(moments) < num_clips:
        end = min(duration, start + chunk)
        if end - start >= min_clip:
            moments.append({"start": start, "end": end, "reason": "time-split"})
        start = end
    return moments


# ── Rendering ────────────────────────────────────────────────────────────

def parse_render_settings(instructions):
    """Parse user instructions into render settings:
    - num_clips: "1 clip", "3 clips"
    - min_clip/max_clip: "de 30 segundos"
    - bg: "fondo negro" -> "black", else "pixel"
    - overlay_text: text inside quotes (to burn on bottom band)
    Returns dict with only the settings explicitly requested."""
    settings = {}
    if not instructions:
        settings["raw"] = True
        return settings
    low = instructions.lower()

    # RAW mode: no processing at all
    if any(kw in low for kw in ["sin editar", "raw", "original", "tal cual", "sin cambios", "sin edición",
                                 "descárgalo", "descargalo", "almacena", "guárdalo", "guardalo", "lo almacenas",
                                 "descargar y", "guardar en el pc", "almacenar en el pc"]):
        settings["raw"] = True
        return settings

    # SUMMARY mode: horizontal concat of key moments
    if any(kw in low for kw in ["resumen", "summary", "recopilación", "recopilacion"]):
        settings["mode"] = "summary"
        m = re.search(r"(\d+)\s*(?:min|minutos|minuto)", low)
        if m:
            settings["summary_duration"] = int(m.group(1)) * 60

    # CTA overlay at end of video (follow call-to-action)
    cta_kw = ["cta", "sígueme", "sigueme", "follow", "llamada a la acción", "llamada a la accion",
              "sigue mi perfil", "síguelo", "siguelo", "no te pierdas", "sígueme para más"]
    if any(kw in low for kw in cta_kw):
        settings["cta"] = True
        # Custom CTA text in quotes — extract from ORIGINAL text to keep case
        m = re.search(r'["""]\s*([^"""]+)\s*["""]', instructions)
        if m:
            settings["cta_text"] = m.group(1).strip()

        # CTA background color: "fondo <color>" or bare color word
        m = re.search(r'fondo\s+(\w+)', low)
        if m:
            col = m.group(1)
            if _color_to_rgb(col) or col in ("blanco", "negro", "white", "black"):
                settings["cta_bg"] = col

    m = re.search(r"(\d+)\s*clip", low)
    if m:
        settings["num_clips"] = int(m.group(1))

    # Duration: "de 20 a 90 segundos", "de 90 segundos", "de 2 minutos",
    # "20 a 90s", "2 min". Range wins over single value.
    m = re.search(r"(\d+)\s*(?:a|hasta|-)\s*(\d+)\s*(?:segundos?|s|min|minutos?)\b", low)
    if m:
        settings["min_clip"] = int(m.group(1))
        settings["max_clip"] = int(m.group(2))
    else:
        m = re.search(r"(\d+)\s*(?:segundos?|s)\b", low)
        if m:
            d = int(m.group(1))
            settings["min_clip"] = d
            settings["max_clip"] = d
        else:
            m = re.search(r"(\d+)\s*(?:minutos?|min)\b", low)
            if m:
                d = int(m.group(1)) * 60
                settings["min_clip"] = d
                settings["max_clip"] = d

    if "horizontal" in low or "sin fondo" in low or "16:9" in low or "16/9" in low:
        settings["bg"] = "none"
    elif "fondo blanco" in low:
        settings["bg"] = "white"
    elif "fondo negro" in low:
        settings["bg"] = "black"
    else:
        m = re.search(r"fondo\s+(\w+)", low)
        if m:
            settings["bg"] = m.group(1)

    if "dinamico" in low or "dynamic" in low:
        settings["dynamic"] = True

    # Subtitles: burned-in captions via subtitle_engine (Whisper GPU)
    # Keywords: subtitulos, subtitles, subs
    sub_kw = ["subtitulos", "subtítulos", "subtitulos", "subtitular",
              "subtitles", "subs", "con subtitulo", "con subtítulos"]
    if any(kw in low for kw in sub_kw):
        settings["subtitles"] = True
        # Level: word vs phrase (default: phrase)
        if any(kw in low for kw in ["palabra", "word", "karaoke", "por palabra"]):
            settings["subtitles_level"] = "word"
        else:
            settings["subtitles_level"] = "phrase"
        # NOTE: subtitle design is fixed (white text, black box). Only the
        # position and language are user-configurable.
        # Language filter
        if "idioma es" in low or "solo espanol" in low or "solo español" in low or "en espanol" in low or "en español" in low:
            settings["subtitles_lang"] = ["es"]
        elif "idioma en" in low or "solo ingles" in low or "solo inglés" in low or "en ingles" in low or "en inglés" in low:
            settings["subtitles_lang"] = ["en"]
        elif any(kw in low for kw in ["ambos idiomas", "bilingue", "bilingüe", "dos idiomas", "es y en", "en y es"]):
            settings["subtitles_lang"] = ["es", "en"]  # explicit bilingual
        else:
            settings["subtitles_lang"] = None  # default: auto-detect (single pass)

        # Subtitle position
        if "abajo" in low or "inferior" in low:
            settings["subtitles_position"] = 0.75
        elif "arriba" in low or "superior" in low:
            settings["subtitles_position"] = 0.35
        elif "en medio" in low or "centro" in low or "centrado" in low:
            settings["subtitles_position"] = 0.50

    # Overlay text burned in clip — only when it's NOT a CTA (CTA has its own text)
    if not settings.get("cta") or not settings.get("cta_text"):
        m = re.search(r'texto\s*["\u201c]\s*([^"\u201d]+)', instructions)
        if m:
            settings["overlay_text"] = m.group(1).strip()
            if "negro" in low and "fondo blanco" in low:
                settings["overlay_color"] = "black"

    # DEFAULT: when instructions are present but no clip count, use 1 clip
    # (don't cut unless user explicitly asks with "N clips")
    if not settings.get("raw") and settings.get("mode") != "summary":
        settings.setdefault("num_clips", 1)

    return settings


def _color_to_rgb(name):
    """Convert color name to RGB tuple. Returns None if unknown."""
    named = {
        "white": (255,255,255), "black": (0,0,0),
        "rojo": (255,0,0), "red": (255,0,0),
        "azul": (0,0,255), "blue": (0,0,255),
        "verde": (0,128,0), "green": (0,128,0),
        "amarillo": (255,255,0), "yellow": (255,255,0),
        "naranja": (255,165,0), "orange": (255,165,0),
        "morado": (128,0,128), "purple": (128,0,128),
        "rosa": (255,192,203), "pink": (255,192,203),
        "gris": (128,128,128), "gray": (128,128,128),
        "cian": (0,255,255), "cyan": (0,255,255),
        "marron": (139,69,19), "brown": (139,69,19),
        "turquesa": (64,224,208), "turquoise": (64,224,208),
        "violeta": (238,130,238), "violet": (238,130,238),
        "dorado": (255,215,0), "gold": (255,215,0),
        "plata": (192,192,192), "silver": (192,192,192),
        "beige": (245,245,220),
        "coral": (255,127,80),
    }
    return named.get(name.lower())


def _face_tracker_available():
    try:
        import face_tracker  # noqa
        return True
    except Exception:
        return False


def make_dynamic_panoramic(clip, trajectory, overlay_text=None, overlay_color="white", target_size=None):
    """Speaker-following panoramic: fg is wider than the frame and slides
    horizontally to keep the detected face visible. Falls back to centered
    if trajectory is empty."""
    if target_size is None:
        target_size = (_FINAL_W, _FINAL_H)
    W, H = target_size
    size = (W, H)
    base = clip.resized((max(80, W // 8), max(144, H // 8))).resized(size)

    # Widen fg so there is room to slide horizontally (face-follow effect)
    FG_W = int(1400 * W / _FINAL_W)
    fg = clip.resized(width=FG_W)

    # 16:9 fg of width FG_W; center vertically
    top = (H - FG_W * 9 / 16) / 2

    def pos(t):
        if not trajectory:
            return ("center", "center")
        x = 0.5
        for i in range(len(trajectory) - 1):
            if trajectory[i][0] <= t <= trajectory[i + 1][0]:
                span = trajectory[i + 1][0] - trajectory[i][0]
                frac = (t - trajectory[i][0]) / span if span > 0 else 0
                x = trajectory[i][1] + (trajectory[i + 1][1] - trajectory[i][1]) * frac
                break
        else:
            x = trajectory[-1][1] if trajectory else 0.5
        # x in [0,1] on source width. Slide window of width W over fg of width FG_W:
        # max slide = FG_W - W. Map face to window position:
        max_slide = FG_W - W
        slide = (x - 0.5) * max_slide
        slide = max(-max_slide / 2, min(max_slide / 2, slide))
        return (W / 2 + slide, top)

    fg = fg.with_position(pos)
    layers = [base, fg]
    if overlay_text:
        from moviepy import TextClip
        font_candidates = [
            os.path.join(_PROJECT_DIR, "fonts", "bold_font.ttf"),
            os.path.join(_PROJECT_DIR, "fonts", "BebasNeue-Regular.ttf"),
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
        font_path = next((f for f in font_candidates if os.path.exists(f)), None)
        txt = (
            TextClip(
                text=overlay_text,
                font=font_path,
                font_size=max(20, int(44 * W / _FINAL_W)),
                color=overlay_color,
                stroke_color="white" if overlay_color == "black" else "black",
                stroke_width=max(1, int(2 * W / _FINAL_W)),
                text_align="center",
            )
            .with_duration(clip.duration)
            .with_position(("center", int(1500 * H / _FINAL_H)))
        )
        layers.append(txt)
    return CompositeVideoClip(layers, size=size)


def make_panoramic(clip, bg="pixel", overlay_text=None, overlay_color="white", dynamic_trajectory=None, target_size=None):
    """Convert any clip to 1080x1920 panoramic.
    bg="none" -> original horizontal clip, no conversion.
    bg="pixel" -> pixelated video background (default).
    bg="white"/"black"/"rojo"/etc -> solid color background.
    overlay_text -> TextClip burned at bottom band (ignored when bg="none").
    dynamic_trajectory -> speaker-following crop (ignored when bg != pixel/none).
    target_size -> render resolution; if smaller than (1080,1920), ffmpeg
    upscales at encode time (much faster Python frame loop, pixel bg hides loss)."""
    if bg == "none":
        return clip

    if target_size is None:
        target_size = (_FINAL_W, _FINAL_H)
    W, H = target_size
    size = (W, H)
    rgb = _color_to_rgb(bg)
    if rgb:
        from moviepy import ColorClip
        base = ColorClip(size=size, color=rgb).with_duration(clip.duration)
    else:
        # pixelated bg: scale down hard first so blocks are visible after upscale
        base = clip.resized((max(80, W // 8), max(144, H // 8))).resized(size)

    if dynamic_trajectory and bg != "none" and _face_tracker_available():
        result = make_dynamic_panoramic(clip, dynamic_trajectory,
                                        overlay_text=overlay_text, overlay_color=overlay_color,
                                        target_size=target_size)
        # Preserve audio through the dynamic composite (CompositeVideoClip
        # drops audio when no audio layer is explicitly added)
        if clip.audio is not None:
            result = result.with_audio(clip.audio)
        return result

    fg = clip.resized(width=W).with_position("center")

    layers = [base, fg]
    if overlay_text:
        from moviepy import TextClip
        font_candidates = [
            os.path.join(_PROJECT_DIR, "fonts", "bold_font.ttf"),
            os.path.join(_PROJECT_DIR, "fonts", "BebasNeue-Regular.ttf"),
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
        font_path = next((f for f in font_candidates if os.path.exists(f)), None)
        txt = (
            TextClip(
                text=overlay_text,
                font=font_path,
                font_size=max(20, int(44 * W / _FINAL_W)),
                color=overlay_color,
                stroke_color="white" if overlay_color == "black" else "black",
                stroke_width=max(1, int(2 * W / _FINAL_W)),
                text_align="center",
            )
            .with_duration(clip.duration)
            .with_position(("center", int(1500 * H / _FINAL_H)))
        )
        layers.append(txt)
    # CompositeVideoClip drops audio — reattach from the foreground clip
    result = CompositeVideoClip(layers, size=size)
    if fg.audio is not None:
        result = result.with_audio(fg.audio)
    return result


def _detect_encoder():
    """Detect best available video encoder with real test: NVENC > QSV > AMF > CPU."""
    def _test(codec, extra):
        try:
            test_dir = tempfile.gettempdir()
            test_in = os.path.join(test_dir, "enc_test_in.mp4")
            test_out = os.path.join(test_dir, "enc_test_out.mp4")
            if not os.path.exists(test_in):
                cmd = [sys.executable, "-c", _GENERATE_TEST_VIDEO_SCRIPT, test_in]
                subprocess.run(cmd, capture_output=True, timeout=30)
            cmd = ["ffmpeg", "-y", "-i", test_in, "-c:v", codec, *extra,
                   "-t", "0.5", "-an", test_out]
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            ok = r.returncode == 0 and os.path.exists(test_out) and os.path.getsize(test_out) > 100
            try:
                os.remove(test_out)
            except Exception:
                pass
            return ok
        except Exception:
            return False

    try:
        r = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True, timeout=15)
        out = r.stdout or ""
        if "h264_nvenc" in out and _test("h264_nvenc", ["-preset", "p4"]):
            return "h264_nvenc"
        if "h264_qsv" in out and _test("h264_qsv", []):
            return "h264_qsv"
        if "h264_amf" in out and _test("h264_amf", ["-quality", "quality"]):
            return "h264_amf"
    except Exception:
        pass
    return "libx264"


_GENERATE_TEST_VIDEO_SCRIPT = """
import sys, subprocess, os
out = sys.argv[1]
# Generate a tiny test video with ffmpeg (1s, 320x240)
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
                "-pix_fmt", "yuv420p", out], capture_output=True, timeout=30)
"""


_ENCODER_CACHE = None
def _get_encoder():
    global _ENCODER_CACHE
    if _ENCODER_CACHE is None:
        _ENCODER_CACHE = _detect_encoder()
    return _ENCODER_CACHE


def process_clip(video_path, clip_start, clip_end, clip_idx, output_dir, bg="pixel", overlay_text=None, overlay_color="white", dynamic=False, cta=False, cta_text=None, cta_bg="white", subtitles=False, subtitles_level="phrase", subtitles_lang=None, subtitles_position=None):
    """Render one clip in panoramic format. Uses GPU encoder when available.
    If dynamic=True and bg is panoramic, tracks faces for speaker-following crop."""
    clip_dur = clip_end - clip_start
    info(f"Clip {clip_idx}: {clip_start:.0f}s - {clip_end:.0f}s ({clip_dur:.0f}s)")

    trajectory = None
    if dynamic and bg not in ("none",):
        try:
            import face_tracker
            positions = face_tracker.track_faces(video_path, clip_start, clip_end)
            if positions:
                trajectory = face_tracker.smooth_trajectory(positions, clip_dur)
                ok(f"  Face tracking: {len(positions)} samples -> dynamic crop")
            else:
                warn("  No faces detected, static center")
        except Exception as e:
            warn(f"  Face tracking failed ({e}), static center")

    clip = VideoFileClip(video_path).subclipped(clip_start, clip_end)
    # Render at half resolution: Python composite loop is the bottleneck (4x
    # fewer pixels/frame), ffmpeg upscales to 1080x1920 during encode.
    render_size = (_RENDER_W, _RENDER_H)
    clip = make_panoramic(clip, bg=bg, overlay_text=overlay_text, overlay_color=overlay_color,
                          dynamic_trajectory=trajectory, target_size=render_size)

    # ── Subtitles: Whisper GPU transcription + burned-in captions ──────
    if subtitles:
        from subtitle_engine import transcribe_segment, render_subtitles, extract_audio_segment
        tmp_wav = extract_audio_segment(video_path, clip_start, clip_end, output_dir)
        try:
            word_level = (subtitles_level == "word")
            lang = subtitles_lang
            ok(f"  Transcribing clip audio ({'/'.join(lang) if lang else 'auto'}, {'word' if word_level else 'phrase'})...")
            entries = transcribe_segment(tmp_wav, word_level=word_level, languages=lang)
            if entries:
                # Unified subtitle style (white text, black box) lives in
                # subtitle_engine.DEFAULT_STYLE — only position is configurable.
                style = {}
                if subtitles_position is not None:
                    style["position"] = subtitles_position
                clip = render_subtitles(clip, entries, style)
                ok(f"  Subtitles burned: {len(entries)} entries")
            else:
                warn("  No subtitle entries produced")
        except Exception as e:
            warn(f"  Subtitles failed ({str(e)[:120]}), continuing without")
        finally:
            if os.path.exists(tmp_wav):
                os.remove(tmp_wav)

    # ── CTA: full-screen blank card AFTER the clip (2s) ────────────────
    if cta:
        from moviepy import TextClip, ColorClip, concatenate_videoclips
        cta_dur = 2.0
        default_cta = "Sígueme para más clips"
        cta_msg = cta_text or default_cta
        font_path = os.path.join(ROOT_DIR, "fonts", "Arial.ttf")
        if not os.path.exists(font_path):
            font_path = "C:/Windows/Fonts/arial.ttf"

        # Full-screen card at render size, user-selectable background color
        if cta_bg in ("blanco", "white"):
            bg_rgb = (255, 255, 255)
        elif cta_bg in ("negro", "black"):
            bg_rgb = (0, 0, 0)
        else:
            bg_rgb = _color_to_rgb(cta_bg) or (255, 255, 255)

        card = ColorClip(
            size=render_size, color=bg_rgb
        ).with_duration(cta_dur)

        # Auto-contrast text: dark bg -> white text, light bg -> black text
        luminance = 0.299 * bg_rgb[0] + 0.587 * bg_rgb[1] + 0.114 * bg_rgb[2]
        txt_color = "#FFFFFF" if luminance < 128 else "#000000"

        # Centered text on the card
        txt = TextClip(
            text=cta_msg, font=font_path,
            font_size=max(28, int(52 * _RENDER_W / 540)),
            color=txt_color, stroke_color=txt_color, stroke_width=0,
            method="caption", size=(int(_RENDER_W * 0.85), int(_RENDER_H * 0.5)),
            text_align="center",
        ).with_position(("center", "center")).with_duration(cta_dur)

        cta_card = CompositeVideoClip([card, txt])
        # Append AFTER the main clip
        clip = concatenate_videoclips([clip, cta_card])

    if clip.audio is not None:
        clip = clip.with_effects([afx.MultiplyVolume(0.85)])

    output_path = os.path.join(output_dir, f"tiktok_clip_{clip_idx}.mp4")
    encoder = _get_encoder()
    info(f"Using encoder: {encoder}")
    # ffmpeg upscales the 540x960 composite to 1080x1920 while encoding
    upscale = ["-vf", f"scale={_FINAL_W}:{_FINAL_H}"]
    try:
        if encoder == "h264_nvenc":
            clip.write_videofile(
                output_path, codec="h264_nvenc", audio_codec="aac",
                ffmpeg_params=["-preset", "p4", *upscale],
                fps=30,
            )
        elif encoder == "h264_qsv":
            clip.write_videofile(
                output_path, codec="h264_qsv", audio_codec="aac",
                ffmpeg_params=["-global_quality", "23", *upscale],
                fps=30,
            )
        elif encoder == "h264_amf":
            clip.write_videofile(
                output_path, codec="h264_amf", audio_codec="aac",
                ffmpeg_params=["-quality", "quality", *upscale],
                fps=30,
            )
        else:
            threads = get_threads()
            clip.write_videofile(
                output_path, codec="libx264", audio_codec="aac",
                threads=threads, preset="medium", fps=30,
                ffmpeg_params=upscale,
            )
    except Exception as e:
        if encoder not in ("libx264",):
            warn(f"GPU encoder {encoder} failed ({str(e)[:120]}). Falling back to libx264 (CPU)...")
            global _ENCODER_CACHE
            _ENCODER_CACHE = "libx264"
            threads = get_threads()
            if os.path.exists(output_path):
                os.remove(output_path)
            clip.write_videofile(
                output_path, codec="libx264", audio_codec="aac",
                threads=threads, preset="medium", fps=30,
                ffmpeg_params=upscale,
            )
        else:
            raise
    clip.close()
    ok(f"  Saved: {output_path}")
    return output_path


# ── Main pipeline ────────────────────────────────────────────────────────

def _get_duration(video_path):
    """Return video duration in seconds (no ffprobe dependency)."""
    try:
        clip = VideoFileClip(video_path)
        d = clip.duration
        clip.close()
        return d
    except Exception:
        return 0


def _concat_raw_moments(video_path, moments, output_path=None):
    """Concat video segments without re-encoding (ffmpeg -c copy, instant).
    Used by SUMMARY mode. Returns output path."""
    import subprocess
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    if output_path is None:
        output_path = os.path.join(mp_dir, f"summary_{uuid.uuid4().hex[:6]}.mp4")
    txt = os.path.join(mp_dir, f"_concat_{uuid.uuid4().hex[:6]}.txt")
    with open(txt, "w", encoding="utf-8") as f:
        for m in moments:
            f.write(f"file '{video_path}'\ninpoint {m['start']}\noutpoint {m['end']}\n")
    ffmpeg = _find_local_ffmpeg()
    subprocess.run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", txt,
                    "-c", "copy", "-movflags", "+faststart", output_path],
                   check=True, capture_output=True)
    os.remove(txt)
    return output_path


def _find_local_ffmpeg():
    """Return bundled ffmpeg path if present, else 'ffmpeg' from PATH."""
    bundled = os.path.join(ROOT_DIR, ".mp", "tools", "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled
    return "ffmpeg"


def main_stream(youtube_url, min_clip=5, max_clip=0, num_clips=3, reporter=None, instructions=None,
                default_bg="pixel", default_overlay_text=None):
    """Download video, extract captions, find viral moments via DeepSeek,
    render clips in panoramic format.
    `instructions` (optional) = user directives passed to DeepSeek as priorities.
    `default_bg` = fallback background when instructions don't specify one.
    `default_overlay_text` = fallback overlay text.
    max_clip=0 (default) = no upper limit; slicing decided by instructions."""
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    os.makedirs(mp_dir, exist_ok=True)

    # Render settings parsed from user instructions (may override clip params)
    render = parse_render_settings(instructions)

    # ── RAW MODE: download and pass through unedited ───────────────────
    if render.get("raw"):
        info("RAW mode: downloading video without processing")
        if reporter:
            reporter.update("Descargando video", 5, "Conectando...")
        video_path, _ = download_media(youtube_url, mp_dir)
        dur = _get_duration(video_path)
        yield {"path": video_path, "duration": dur, "index": 1, "bg": "raw",
               "description": "", "tags": [], "source_video": video_path,
               "moment_start": 0, "moment_end": dur}
        if reporter:
            reporter.update("Video listo", 95, "raw")
        return

    # ── SUMMARY MODE: horizontal concat of IA-selected moments ─────────
    if render.get("mode") == "summary":
        info("SUMMARY mode: building horizontal summary")
        if reporter:
            reporter.update("Descargando video", 5, "Conectando...")
        video_path, caption_path = download_media(youtube_url, mp_dir)
        segments = parse_vtt(caption_path) if caption_path else []
        dur = _get_duration(video_path)
        target = render.get("summary_duration", 300)
        if segments:
            instructions = instructions or ""
            moments, desc, tags = find_best_moments(
                segments, dur, min_clip=10, max_clip=90,
                num_clips=max(4, min(12, target // 30)), instructions=instructions)
        else:
            moments, desc, tags = [], "", []
        if not moments:
            warn("No moments found, using time-based split")
            moments = _split_timebased(dur, 10, 90, max(4, min(12, target // 30)))
        out_path = _concat_raw_moments(video_path, moments)
        yield {"path": out_path, "duration": sum(m["end"]-m["start"] for m in moments),
               "index": 1, "bg": "none", "description": desc, "tags": tags,
               "source_video": video_path, "moment_start": 0, "moment_end": 0}
        if reporter:
            reporter.update("Resumen listo", 95, "summary")
        return

    num_clips = render.get("num_clips", num_clips)
    min_clip = render.get("min_clip", min_clip)
    max_clip = render.get("max_clip", max_clip)
    bg = render.get("bg", default_bg)
    overlay_text = render.get("overlay_text", default_overlay_text)
    overlay_color = render.get("overlay_color", "white")
    dynamic = render.get("dynamic", False)
    cta = render.get("cta", False)
    cta_text = render.get("cta_text")
    cta_bg = render.get("cta_bg", "white")
    subtitles = render.get("subtitles", False)
    subtitles_level = render.get("subtitles_level", "phrase")
    subtitles_lang = render.get("subtitles_lang", ["es", "en"])
    subtitles_position = render.get("subtitles_position")
    if overlay_text:
        warn(f"Overlay text: {overlay_text} ({overlay_color})")
    if bg != "pixel":
        info(f"Background: {bg}")
    if dynamic:
        info("Dynamic face tracking ON")
    info(f"Params: {num_clips} clips, {min_clip}s-{max_clip}s, bg={bg}")

    # 1. Download video + captions
    if reporter:
        reporter.update("Descargando video", 5, "Conectando...")
    video_path, caption_path = download_media(youtube_url, mp_dir)

    # 2. Parse captions
    if reporter:
        reporter.update("Analizando transcripción", 20)
    segments = parse_vtt(caption_path)
    if not segments:
        warn("No captions found, using time-based split")
    else:
        ok(f"Parsed {len(segments)} caption segments")

    # 3. Get video duration
    clip_info = VideoFileClip(video_path)
    video_duration = clip_info.duration
    clip_info.close()
    info(f"Video duration: {video_duration:.0f}s")

    # 4. Find viral moments
    description, tags = "", []
    if segments:
        if reporter:
            reporter.update("Buscando mejores momentos con DeepSeek", 30)
        moments, description, tags = find_best_moments(segments, video_duration, min_clip, max_clip, num_clips, instructions)
    else:
        moments = []

    # Fallback: time-based split
    if not moments:
        warn("No moments found. Using time-based split as fallback.")
        moments = _split_timebased(video_duration, min_clip, max_clip, num_clips)

    # 5. Render clips
    total = len(moments)
    for i, m in enumerate(moments):
        if reporter:
            pct = 40 + int((i / total) * 50)
            reporter.update(f"Renderizando clip {i+1} de {total}", pct,
                          f"{m['end']-m['start']:.0f}s {bg}")

        out = process_clip(video_path, m["start"], m["end"], i + 1, mp_dir,
                           bg=bg, overlay_text=overlay_text, overlay_color=overlay_color, dynamic=dynamic,
                           cta=cta, cta_text=cta_text, cta_bg=cta_bg,
                           subtitles=subtitles, subtitles_level=subtitles_level,
                           subtitles_lang=subtitles_lang,
                           subtitles_position=subtitles_position)
        yield {"path": out, "duration": m["end"] - m["start"], "index": i + 1, "bg": bg,
               "description": description, "tags": tags,
               "source_video": video_path, "moment_start": m["start"], "moment_end": m["end"]}

    if reporter:
        reporter.update("Clips generados", 95, f"{total} clips listos")


def main(youtube_url, min_clip=5, max_clip=0, num_clips=4, instructions=None):
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    os.makedirs(mp_dir, exist_ok=True)

    render = parse_render_settings(instructions)
    num_clips = render.get("num_clips", num_clips)
    min_clip = render.get("min_clip", min_clip)
    max_clip = render.get("max_clip", max_clip)
    bg = render.get("bg", "pixel")
    overlay_text = render.get("overlay_text")
    overlay_color = render.get("overlay_color", "white")
    cta = render.get("cta", False)
    cta_text = render.get("cta_text")
    cta_bg = render.get("cta_bg", "white")
    subtitles = render.get("subtitles", False)
    subtitles_level = render.get("subtitles_level", "phrase")
    subtitles_lang = render.get("subtitles_lang", ["es", "en"])
    subtitles_position = render.get("subtitles_position")

    video_path, caption_path = download_youtube(youtube_url, mp_dir)
    segments = parse_vtt(caption_path)

    clip_info = VideoFileClip(video_path)
    video_duration = clip_info.duration
    clip_info.close()

    if segments:
        moments, description, tags = find_best_moments(segments, video_duration, min_clip, max_clip, num_clips, instructions)
    else:
        moments, description, tags = [], "", []
    if not moments:
        moments = _split_timebased(video_duration, min_clip, max_clip, num_clips)

    outputs = []
    for i, m in enumerate(moments):
        out = process_clip(video_path, m["start"], m["end"], i + 1, mp_dir,
                           bg=bg, overlay_text=overlay_text, overlay_color=overlay_color,
                           cta=cta, cta_text=cta_text, cta_bg=cta_bg,
                           subtitles=subtitles, subtitles_level=subtitles_level,
                           subtitles_lang=subtitles_lang,
                           subtitles_position=subtitles_position)
        outputs.append({"path": out, "duration": m["end"] - m["start"],
                        "description": description, "tags": tags})

    return outputs


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TikTok Panoramic Clips")
    parser.add_argument("url", nargs="?", help="YouTube URL")
    parser.add_argument("--min", type=int, default=20, help="Min clip seconds")
    parser.add_argument("--max", type=int, default=60, help="Max clip seconds")
    parser.add_argument("--clips", type=int, default=4, help="Number of clips")
    args = parser.parse_args()

    url = args.url or input("YouTube URL: ").strip()
    if not url:
        err("No URL")
        sys.exit(1)

    outputs = main(url, args.min, args.max, args.clips)
    print(f"\n{'='*50}")
    for o in outputs:
        print(o)
