# Repository Guidelines

## Project Structure & Module Organization
- `src/` contains the application code. Use `src/main.py` as the interactive entrypoint.
- `src/classes/` holds provider-specific components (for example `YouTube.py`, `Twitter.py`, `Tts.py`, `AFM.py`, `Outreach.py`).
- Shared utilities and configuration live in modules like `src/config.py`, `src/utils.py`, `src/cache.py`, and `src/constants.py`.
- `scripts/` contains helper workflows such as setup, preflight checks, upload helpers, and direct Telegram send.
- `docs/` contains feature documentation; `assets/` and `fonts/` contain static resources.

## Build, Test, and Development Commands
- `bash scripts/setup_local.sh`: bootstrap local development (creates `venv`, installs deps, seeds `config.json`, runs preflight).
- `source venv/bin/activate && pip install -r requirements.txt`: manual dependency install/update.
- `python3 scripts/preflight_local.py`: validate local provider/config readiness before running tasks.
- `python3 src/main.py`: start the CLI app.
- `bash scripts/upload_video.sh`: run direct script-based upload flow from repo root.

## Coding Style & Naming Conventions
- Target Python 3.12 (project requirement in `README.md`).
- Use 4-space indentation and follow existing Python conventions:
  - `snake_case` for functions/variables
  - `PascalCase` for classes
  - `UPPER_SNAKE_CASE` for constants
- Keep new business logic in focused modules under `src/`; keep provider/integration code in `src/classes/`.
- Prefer small, explicit functions and preserve existing CLI-first behavior.

## Testing Guidelines
- There is currently no enforced automated test suite or coverage threshold.
- Minimum validation for changes:
  - Run `python3 scripts/preflight_local.py`
  - Smoke-test impacted flows via `python3 src/main.py`
- When adding tests, place them in a top-level `tests/` directory with names like `test_<module>.py`.

## Commit & Pull Request Guidelines
- Follow the existing commit style: imperative summaries like `Fix ...`, `Update ...`, optionally with issue refs (for example `(#128)`).
- Open PRs against `main`.
- Link each PR to an issue, keep scope to one feature/fix, and use a clear title + description.
- Mark not-ready PRs with `WIP` and remove it when ready for review.

## Security & Configuration Tips
- Treat `config.json` as environment-specific; do not commit real API keys or private profile paths.
- Start from `config.example.json` and prefer environment variables where supported (for example `GEMINI_API_KEY`).

---

## 🎬 Video Processing Workflows

### Clip types & when to use each

| Type | Format | Use | Code path |
|---|---|---|---|
| Panoramic (TikTok) | 9:16, 1080×1920 | Vertical clips with pixel/colored bg | `tiktok_clips.process_clip()` with `bg="pixel"` |
| Dynamic face-track | 9:16, 1080×1920 | Follows speaker's face | `process_clip(dynamic=True)` + `face_tracker.py` |
| Horizontal (no bg) | 16:9, 1920×1080 | Raw horizontal cuts, summaries | ffmpeg `-c copy` concat (no moviepy) |
| With subtitles | any | Burned-in captions | `test_subtitles_clip.py` pattern: Whisper GPU → SRT → `SubtitlesClip` + `CompositeVideoClip` |

### Summary generation (horizontal, IA-selected moments)
**Script**: `scripts/summarize_video.py <youtube_url> [--duration 300] [--send]`

Pipeline:
1. `yt-dlp` downloads video + auto-subs (VTT)
2. `parse_vtt()` → `find_best_moments()` (DeepSeek) picks key moments
3. ffmpeg `-c copy` concat (NO re-encode, instant, original quality)
4. File stays under 50MB if source video ~400MB — check size before sending
5. `scripts/send_telegram.py` sends directly (bypasses bot worker)

**Why `-c copy`**: Re-encoding 4min of 1080p with NVENC takes ~200s and easily exceeds 50MB. `-c copy` is instant and proportional to source (e.g., 240s/2189s × 408MB = ~45MB).

### Sending directly to Telegram (bypass bot)
**Script**: `scripts/send_telegram.py <video.mp4> [caption]`

Config: `.mp/telegram.json` → `{bot_token, chat_id}`. Limit: **50MB** (Bot API).  
Use this for manual renders, summaries, subtitle jobs — no need to enqueue in `jobs.db`.

### Adding subtitles to existing clips
Pattern (see `test_subtitles_clip.py`):
1. Extract audio: `ffmpeg -vn -acodec pcm_s16le -ar 16000 -ac 1 audio.wav`
2. Transcribe: `transcribe_audio()` from `tiktok_video.py` (faster-whisper, GPU)
3. SRT → `SubtitlesClip(generator)` with Arial font, position bottom-140
4. `CompositeVideoClip([video, subtitles])`, render NVENC
5. Send: `scripts/send_telegram.py`

---

## 🖥️ GPU / NVENC Setup

**ffmpeg 6.1.1 bundle** in `.mp/tools/` — required for NVIDIA drivers < 551.76.  
- Driver 546.18 → nvenc API 12.1 → ffmpeg 8.x fails with "Invalid argument" (-22)  
- Bundle: downloaded from gyan.dev 6.1.1-essentials, ffmpeg.exe + ffprobe.exe (~79MB each)
- Config: `tiktok_clips.py` prepends `.mp/tools/` to PATH + sets `FFMPEG_BINARY` env + `moviepy.config.FFMPEG_BINARY` override (MoviePy 2.1.2: `change_settings` does NOT exist)
- Binaries gitignored (`.mp/` and `*.exe` in `.gitignore`)

**Half-res render pipeline** (2.2× speedup):
- Composite at `_RENDER_W=540, _RENDER_H=960` (4× fewer pixels)
- Encode with `-vf scale=1080:1920` (upscale done by NVENC)
- Pixelated bg hides quality loss; font sizes/positions scale with `W/_FINAL_W` ratio
- Encoder priority: nvenc > qsv > amf > libx264

---

## 🤖 Bot Internals

### Architecture
- `telegram_bot.py`: PTB 22.8, main thread runs `app.run_polling()`, worker thread processes `jobs.db`
- `web_ui.py`: Flask dashboard at `127.0.0.1:5050`
- `start_bot_launcher.py`: spawns both as subprocesses, writes to `bot_output.log`
- Queue: `job_queue.py` → SQLite `jobs.db` (survives restarts — `main()` resets 'processing'→'pending')

### Critical fix: PTB 22.x has NO `app.loop`
PTB 22.8 removed `Application.loop`. Worker threads must NOT use `app.loop`.  
**Fix**: Capture loop via `post_init` bootstrap:
```python
_app_loop = None  # global
async def _capture_loop(app):
    global _app_loop
    _app_loop = asyncio.get_running_loop()

app = Application.builder().token(TOKEN).post_init(_capture_loop).build()
```
Then in worker: `asyncio.run_coroutine_threadsafe(coro, _app_loop).result(timeout=120)`  
This is committed in `telegram_bot.py` (commit `fd68855`).

### User rules (critical)
- **Kill bot + restart from scratch after every code change**  
  `wmic process where "name='python.exe'" call terminate` → `start_bot_launcher.py`
- Process count: 4 python.exe = 2 scripts × (venv shim + real interpreter). venv python.exe is a launcher shim — this is NORMAL.

### Configuration
- Per-chat settings: `bot_config.py` → `.mp/bot_config.json`  
  Defaults: `num_clips=3, min_clip=20, max_clip=60, bg="pixel"`
- Telegram token + chat_id: `.mp/telegram.json`  
- GPU/Whisper: `config.py` → `get_whisper_model()` (medium), `get_whisper_device()` (cuda)

---

## 🐛 Common Gotchas

| Issue | Symptom | Fix |
|---|---|---|
| NVENC "Invalid argument" | Errno 22, NVENC fails silently | Driver < 551.76: use ffmpeg 6.1.1 bundle in `.mp/tools/` |
| PTB 22.x `app.loop` missing | `'Application' object has no attribute 'loop'` | Capture loop via `post_init` (see above) |
| MoviePy 2.1.2 `change_settings` | AttributeError | Use `import moviepy.config as c; c.FFMPEG_BINARY = path` |
| Telegram 413 "Entity Too Large" | File > 50MB | Use `-c copy` or increase `-cq` value |
| ffmpeg concat encoding | Non-monotonic DTS warnings | Harmless — `-c copy` avoids re-encode issues entirely |
| Process count "wrong" | 4 python.exe instead of 2 | venv python.exe is a shim that spawns real interpreter — 2 per script = normal |
| `send_message` from worker fails | `'Application' object has no attribute 'loop'` | Same as PTB fix: use `_app_loop` with `run_coroutine_threadsafe` |
| DeepSeek finds 0 moments | No captions or bad transcript | Falls back to `_split_timebased()` in `tiktok_clips.main_stream()` |

---

## 📊 Benchmark Reference (RTX 4060 8GB, driver 546.18)

| Config | Encoder | Speed | Notes |
|---|---|---|---|
| 1080×1920 composite | NVENC | ~5 it/s | Python compositing bottleneck |
| 540×960 → upscale 1080×1920 | NVENC | ~10-13 it/s | Half-res composite, ffmpeg upscale |
| 1080p raw (no composite) | NVENC | ~35 fps | Just encode, no compositing |
| 1080p `-c copy` | none | 400×+ | Instant, no re-encode |
| CPU libx264 | Software | ~2 it/s | Without NVENC |
