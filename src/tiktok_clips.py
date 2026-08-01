"""
TikTok Clip Extractor - MoneyPrinterV2
Downloads YouTube video, extracts auto-captions (no Whisper),
uses DeepSeek to find viral moments, renders in panoramic format
(original 16:9 centered on 9:16 with pixelated video background).
No subtitles rendered, no face tracking.
"""
import os, sys, re, json, uuid, subprocess
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

from moviepy import VideoFileClip, CompositeVideoClip, afx
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
    """Extract YouTube video ID from various URL formats."""
    for pat in (r"v=([\w-]{11})", r"youtu\.be/([\w-]{11})", r"/(?:embed|shorts|v)/([\w-]{11})"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    # Fallback: hash the URL
    return str(uuid.uuid4())[:8]


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

def find_best_moments(segments, video_duration, min_clip=20, max_clip=60, num_clips=3, instructions=None):
    """Use DeepSeek to find most viral moments from captions.
    Then snap boundaries to nearest segment to avoid mid-sentence cuts.
    `instructions` (optional) = user-provided directives that take priority."""
    info("Analyzing captions with DeepSeek to find best moments...")
    if instructions:
        warn(f"User instructions: {instructions[:200]}")

    transcript = ""
    for seg in segments:
        transcript += f"[{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}\n"

    user_rules = f"""
- El video dura {video_duration:.0f}s. Busca {num_clips} clips de {min_clip}s a {max_clip}s.
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
DEVUELVE SOLO JSON:
{{
  "clips": [
    {{"start": 12.0, "end": 38.5, "reason": "explica por que es viral"}}
  ]
}}

Transcripcion con timestamps:
{transcript[:12000]}"""

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
                    return []
            else:
                err(f"DeepSeek returned no JSON: {response[:300]}")
                return []
        return data.get("clips", [])

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
    clips = _parse_response(response)
    validated = _validate(clips)

    # Attempt 2: if 0 valid, retry with a simpler, more forceful prompt
    if not validated:
        warn("DeepSeek returned 0 valid clips, retrying with stricter prompt...")
        retry_prompt = f"""Transcripcion de un video de {video_duration:.0f}s con timestamps.
Devuelve SOLO JSON con {num_clips} clips virales. CADA clip:
- start y end deben ser timestamps EXACTOS de la transcripcion (frases completas, sin cortes)
- duracion entre {min_clip}s y {max_clip}s
- usa los timestamps [X.Xs-Y.Ys] de la transcripcion como referencia

{('INSTRUCCIONES DEL USUARIO (CUMPLELAS): ' + instructions) if instructions else ''}

JSON:
{{"clips": [{{"start": 0.0, "end": 0.0, "reason": ""}}]}}

Transcripcion:
{transcript[:12000]}"""
        response2 = generate_text(retry_prompt)
        clips2 = _parse_response(response2)
        validated = _validate(clips2)

    ok(f"DeepSeek found {len(validated)} viral moments")
    for i, v in enumerate(validated):
        print(f"  [{i+1}] {v['start']:.0f}s-{v['end']:.0f}s ({v['end']-v['start']:.0f}s) | {v['reason'][:80]}")
    return validated


def _split_timebased(duration, min_clip=20, max_clip=60, num_clips=4):
    """Fallback: split video duration into N clips evenly."""
    if max_clip > duration:
        max_clip = duration
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
        return settings
    low = instructions.lower()

    m = re.search(r"(\d+)\s*clip", low)
    if m:
        settings["num_clips"] = int(m.group(1))

    m = re.search(r"(\d+)\s*(?:segundos?|s)\b", low)
    if m:
        d = int(m.group(1))
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

    m = re.search(r'texto\s*["\u201c]\s*([^"\u201d]+)', instructions)
    if m:
        settings["overlay_text"] = m.group(1).strip()
        if "negro" in low and "fondo blanco" in low:
            settings["overlay_color"] = "black"
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


def make_panoramic(clip, bg="pixel", overlay_text=None, overlay_color="white"):
    """Convert any clip to 1080x1920 panoramic.
    bg="none" -> original horizontal clip, no conversion.
    bg="pixel" -> pixelated video background (default).
    bg="white"/"black"/"rojo"/etc -> solid color background.
    overlay_text -> TextClip burned at bottom band (ignored when bg="none")."""
    if bg == "none":
        return clip

    size = (1080, 1920)
    rgb = _color_to_rgb(bg)
    if rgb:
        from moviepy import ColorClip
        base = ColorClip(size=size, color=rgb).with_duration(clip.duration)
    else:
        base = clip.resized((80, 144)).resized(size)

    fg = clip.resized(width=1080).with_position("center")

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
                font_size=44,
                color=overlay_color,
                stroke_color="white" if overlay_color == "black" else "black",
                stroke_width=2,
                text_align="center",
            )
            .with_duration(clip.duration)
            .with_position(("center", 1500))
        )
        layers.append(txt)
    return CompositeVideoClip(layers, size=size)


def process_clip(video_path, clip_start, clip_end, clip_idx, output_dir, bg="pixel", overlay_text=None, overlay_color="white"):
    """Render one clip in panoramic format."""
    clip_dur = clip_end - clip_start
    info(f"Clip {clip_idx}: {clip_start:.0f}s - {clip_end:.0f}s ({clip_dur:.0f}s)")

    clip = VideoFileClip(video_path).subclipped(clip_start, clip_end)
    clip = make_panoramic(clip, bg=bg, overlay_text=overlay_text, overlay_color=overlay_color)

    if clip.audio is not None:
        clip = clip.with_effects([afx.MultiplyVolume(0.85)])

    output_path = os.path.join(output_dir, f"tiktok_clip_{clip_idx}.mp4")
    threads = get_threads()
    clip.write_videofile(
        output_path, codec="libx264", audio_codec="aac",
        threads=threads, preset="medium", fps=30,
    )
    clip.close()
    ok(f"  Saved: {output_path}")
    return output_path


# ── Main pipeline ────────────────────────────────────────────────────────

def main_stream(youtube_url, min_clip=20, max_clip=60, num_clips=3, reporter=None, instructions=None):
    """Download video, extract captions, find viral moments via DeepSeek,
    render clips in panoramic format.
    `instructions` (optional) = user directives passed to DeepSeek as priorities."""
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    os.makedirs(mp_dir, exist_ok=True)

    # Render settings parsed from user instructions (may override clip params)
    render = parse_render_settings(instructions)
    num_clips = render.get("num_clips", num_clips)
    min_clip = render.get("min_clip", min_clip)
    max_clip = render.get("max_clip", max_clip)
    bg = render.get("bg", "pixel")
    overlay_text = render.get("overlay_text")
    overlay_color = render.get("overlay_color", "white")
    if overlay_text:
        warn(f"Overlay text: {overlay_text} ({overlay_color})")
    if bg != "pixel":
        info(f"Background: {bg}")
    info(f"Params: {num_clips} clips, {min_clip}s-{max_clip}s, bg={bg}")

    # 1. Download video + captions
    if reporter:
        reporter.update("Descargando video + subtítulos", 5, "Conectando con YouTube...")
    video_path, caption_path = download_youtube(youtube_url, mp_dir)

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
    if segments:
        if reporter:
            reporter.update("Buscando mejores momentos con DeepSeek", 30)
        moments = find_best_moments(segments, video_duration, min_clip, max_clip, num_clips, instructions)
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
                           bg=bg, overlay_text=overlay_text, overlay_color=overlay_color)
        yield {"path": out, "duration": m["end"] - m["start"], "index": i + 1, "bg": bg}

    if reporter:
        reporter.update("Clips generados", 95, f"{total} clips listos")


def main(youtube_url, min_clip=20, max_clip=60, num_clips=4, instructions=None):
    mp_dir = os.path.join(ROOT_DIR, ".mp")
    os.makedirs(mp_dir, exist_ok=True)

    render = parse_render_settings(instructions)
    num_clips = render.get("num_clips", num_clips)
    min_clip = render.get("min_clip", min_clip)
    max_clip = render.get("max_clip", max_clip)
    bg = render.get("bg", "pixel")
    overlay_text = render.get("overlay_text")
    overlay_color = render.get("overlay_color", "white")

    video_path, caption_path = download_youtube(youtube_url, mp_dir)
    segments = parse_vtt(caption_path)

    clip_info = VideoFileClip(video_path)
    video_duration = clip_info.duration
    clip_info.close()

    if segments:
        moments = find_best_moments(segments, video_duration, min_clip, max_clip, num_clips, instructions)
    else:
        moments = []
    if not moments:
        moments = _split_timebased(video_duration, min_clip, max_clip, num_clips)

    outputs = []
    for i, m in enumerate(moments):
        out = process_clip(video_path, m["start"], m["end"], i + 1, mp_dir,
                           bg=bg, overlay_text=overlay_text, overlay_color=overlay_color)
        outputs.append({"path": out, "duration": m["end"] - m["start"]})

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
