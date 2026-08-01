"""
MoneyPrinter Telegram Bot - reactive bot via python-telegram-bot.
Receives YouTube URLs, generates clips streaming, sends them one by one.
Inline buttons: [Subir a TikTok] [Saltar]
Commands: /help, /config, /clips, /duracion, /fondo, /texto, /horizontal,
          /panoramico, /historial, /reset
"""

import os
import sys
import json
import re
import threading
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from telegram_notify import TELEGRAM_CONFIG
from tiktok_clips import main_stream, is_playlist_url, extract_playlist_urls
from tiktok_uploader import upload_video
from progress_reporter import ProgressReporter
from config import ROOT_DIR
import bot_config
import job_queue
import ab_testing

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(ROOT_DIR, ".mp", "bot_state.json")

with open(TELEGRAM_CONFIG) as f:
    cfg = json.load(f)
TOKEN = cfg["bot_token"]


# ── State helpers (for TikTok upload callbacks) ─────────────────────────

def save_state(chat_id, clip_info):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    state[str(chat_id)] = clip_info
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_state(chat_id):
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        state = json.load(f)
    return state.get(str(chat_id))


def clear_state(chat_id):
    if not os.path.exists(STATE_FILE):
        return
    with open(STATE_FILE) as f:
        state = json.load(f)
    state.pop(str(chat_id), None)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


_URL_DOMAINS = ["youtube.com", "youtu.be", "twitch.tv", "instagram.com", ".mp4", ".webm", ".mov"]


def split_url_and_instructions(text):
    """Split message into (url, instructions). URL = first media link,
    instructions = everything else in the message."""
    urls = [w for w in text.split() if any(d in w for d in _URL_DOMAINS)]
    if not urls:
        return None, text
    url = urls[0]
    instructions = text
    for u in urls:
        instructions = instructions.replace(u, "")
    instructions = re.sub(r"\s+", " ", instructions).strip()
    return url, instructions


# ── Job worker (background thread) ───────────────────────────────────────

_worker_started = False
_worker_lock = threading.Lock()
_application = None  # set in main() so worker can send messages


def _ensure_worker():
    """Start the background job worker if not already running."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        t = threading.Thread(target=_job_worker, daemon=True)
        t.start()
        logger.info("Job worker started")


def _job_worker():
    """Background loop: process jobs from the SQLite queue sequentially."""
    while True:
        job = job_queue.dequeue()
        if job is None:
            # No work: sleep and check again
            import time
            time.sleep(3)
            continue
        try:
            _process_job(job)
        except Exception as e:
            logger.error(f"Job {job['id']} crashed: {e}")
            job_queue.update_status(job["id"], "failed", error=str(e))


def _send_progress_message(chat_id, text, parse_mode="HTML"):
    """Best-effort send to a chat via the application. Safe in worker thread."""
    app = _application
    if app is None:
        return None
    try:
        import asyncio
        return asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode),
            app.loop,
        ).result(timeout=30)
    except Exception as e:
        logger.warning(f"send_message failed: {e}")
        return None


def _process_job(job):
    """Process a single queued job: run main_stream, send clips, upload if auto."""
    chat_id = int(job["chat_id"])
    url = job["url"]
    instructions = job.get("instructions") or ""
    cfg = bot_config.get_all(chat_id)

    logger.info(f"Processing job {job['id']}: {url}")
    reporter = ProgressReporter(chat_id=chat_id)
    reporter.start()
    _send_progress_message(chat_id, f"📥 <b>Procesando job #{job['id']}</b>")

    outputs = []
    try:
        clip_count = 0
        for clip in main_stream(url,
                                min_clip=cfg["min_clip"],
                                max_clip=cfg["max_clip"],
                                num_clips=cfg["num_clips"],
                                reporter=reporter,
                                instructions=instructions or None,
                                default_bg=cfg["bg"],
                                default_overlay_text=cfg.get("overlay_text")):
            clip_count += 1
            dur = clip.get("duration", 0)
            path = clip["path"]
            bg = clip.get("bg", "pixel")
            is_horizontal = (bg == "none")
            description = clip.get("description", "")
            tags = clip.get("tags", [])

            if is_horizontal:
                caption = f"🎬 <b>Clip {clip_count}</b> ({dur:.0f}s) horizontal"
                vid_dim = {}
            else:
                caption = f"🎬 <b>Clip {clip_count}</b> ({dur:.0f}s) panorámico"
                vid_dim = {"width": 1080, "height": 1920}

            if description:
                caption += f"\n\n{description}"
            if tags:
                tags_text = " ".join(f"#{str(t).strip('#')}" for t in tags)
                caption += f"\n\n{tags_text}"

            save_state(chat_id, {
                "path": path,
                "clip_idx": clip_count,
                "description": description,
                "tags": tags,
            })

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("▶ Subir a TikTok", callback_data=f"upload_{clip_count}"),
                    InlineKeyboardButton("⏭ Saltar", callback_data=f"skip_{clip_count}"),
                ]
            ])

            with open(path, "rb") as video_file:
                app = _application
                if app is None:
                    raise RuntimeError("Bot application not initialized")
                import asyncio
                future = asyncio.run_coroutine_threadsafe(
                    app.bot.send_video(
                        chat_id=chat_id,
                        video=video_file,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        supports_streaming=True,
                        **vid_dim,
                    ),
                    app.loop,
                )
                future.result(timeout=120)

            outputs.append(path)

            # Auto-upload mode
            if bot_config.get(chat_id, "auto_upload", False):
                try:
                    desc = description or "Clip automático"
                    upload_video(path, desc, tags, draft=False)
                    _send_progress_message(chat_id, f"✅ Clip {clip_count} subido a TikTok automáticamente")
                except Exception as e:
                    _send_progress_message(chat_id, f"⚠️ Auto-upload falló clip {clip_count}: {str(e)[:100]}")

            # AB testing: render alternative variants for this clip
            if bot_config.get(chat_id, "ab_test", False):
                try:
                    from tiktok_clips import process_clip
                    from config import ROOT_DIR as _RD
                    mp_dir = os.path.join(_RD, ".mp")
                    src_video = clip.get("source_video")
                    m_start = clip.get("moment_start", 0)
                    m_end = clip.get("moment_end", dur)
                    for var in ab_testing.get_variants():
                        if var["name"] == "A":
                            continue  # A is the main clip already rendered
                        if not src_video:
                            break
                        v_bg = var.get("bg", bg)
                        v_text = var.get("overlay_text")
                        alt_out = process_clip(
                            src_video, m_start, m_end, clip_count + 100,
                            mp_dir, bg=v_bg, overlay_text=v_text,
                        )
                        ab_testing.record_test(job["id"], clip_count, var["name"], var, alt_out)
                        if bot_config.get(chat_id, "auto_upload", False):
                            try:
                                upload_video(alt_out, description or "Clip variante", tags, draft=False)
                                _send_progress_message(chat_id, f"🧪 Variante {var['name']} clip {clip_count} subida")
                            except Exception as e2:
                                _send_progress_message(chat_id, f"⚠️ Variante {var['name']} upload falló: {str(e2)[:100]}")
                except Exception as e:
                    _send_progress_message(chat_id, f"⚠️ AB variant falló: {str(e)[:100]}")

        reporter.stop()
        if clip_count == 0:
            _send_progress_message(chat_id, "⚠ No se encontraron momentos para generar clips.")
            job_queue.update_status(job["id"], "failed", error="no clips", num_clips=0)
        else:
            _send_progress_message(chat_id, f"🏁 Job #{job['id']}: {clip_count} clips generados.")
            job_queue.update_status(job["id"], "done", num_clips=clip_count, output_paths=outputs)
        bot_config.add_history(chat_id, url, clip_count)

    except Exception as e:
        logger.error(f"Error processing {url}: {e}")
        reporter.stop()
        _send_progress_message(chat_id, f"❌ Error: {str(e)[:200]}")
        job_queue.update_status(job["id"], "failed", error=str(e))


# ── Command handlers ─────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands."""
    await update.message.reply_text(
        "🎬 <b>MoneyPrinter V2 — Comandos</b>\n\n"
        "<b>Producción:</b>\n"
        "Envía un enlace de YouTube + instrucciones opcionales\n"
        "/queue — Ver cola de trabajos\n"
        "/cancel &lt;id&gt; — Cancelar trabajo pendiente\n"
        "/historial — Últimos trabajos\n"
        "/auto on|off — Subida automática a TikTok\n\n"
        "<b>Configuración:</b>\n"
        "/config — Ver configuración actual\n"
        "/clips &lt;n&gt; — Nº de clips (1-5)\n"
        "/duracion &lt;min&gt; &lt;max&gt; — Rango en segundos\n"
        "/fondo &lt;color|pixel|none&gt; — Fondo por defecto\n"
        "/texto &lt;frase&gt; — Texto overlay por defecto\n"
        "/texto off — Quitar texto overlay\n"
        "/horizontal — Modo horizontal (sin letterbox)\n"
        "/panoramico — Modo panorámico 9:16\n"
        "/reset — Volver a defaults\n\n"
        "<b>Info:</b>\n"
        "/help — Este mensaje\n\n"
        "<b>Colores disponibles:</b> rojo, azul, verde, amarillo, naranja, "
        "morado, rosa, gris, cian, marrón, turquesa, dorado, plata, beige, coral, blanco, negro.",
        parse_mode="HTML",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    await update.message.reply_text(
        "🎬 <b>MoneyPrinter V2 Bot</b>\n\n"
        "Envíame un enlace de YouTube y te generaré clips.\n\n"
        "Por defecto: 3 clips de 20-60s, formato panorámico 9:16.\n\n"
        "Personaliza con comandos (/clips, /fondo, /texto...) "
        "o añade instrucciones junto al enlace.\n\n"
        "/help para ver todos los comandos.",
        parse_mode="HTML",
    )


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current configuration."""
    chat_id = update.effective_chat.id
    c = bot_config.get_all(chat_id)
    lines = [
        f"🎛 <b>Configuración actual</b>",
        f"Clips: <code>{c['num_clips']}</code>",
        f"Duración: <code>{c['min_clip']}s – {c['max_clip']}s</code>",
        f"Fondo: <code>{c['bg']}</code>",
        f"Texto: <code>{c.get('overlay_text') or 'ninguno'}</code>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_clips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set number of clips: /clips 3"""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Uso: /clips &lt;número&gt;  (1-5)")
        return
    try:
        n = int(context.args[0])
        n = max(1, min(5, n))
        bot_config.set(chat_id, "num_clips", n)
        await update.message.reply_text(f"✅ Clips por defecto: <b>{n}</b>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("Número inválido. Ej: /clips 3")


async def cmd_duracion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set duration range: /duracion 20 60"""
    chat_id = update.effective_chat.id
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /duracion &lt;min&gt; &lt;max&gt;  (segundos)")
        return
    try:
        mn, mx = int(context.args[0]), int(context.args[1])
        mn, mx = max(5, mn), max(mn + 5, mx)
        bot_config.set(chat_id, "min_clip", mn)
        bot_config.set(chat_id, "max_clip", mx)
        await update.message.reply_text(f"✅ Duración: <b>{mn}s – {mx}s</b>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("Valores inválidos. Ej: /duracion 20 60")


async def cmd_fondo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set default background: /fondo rojo  or  /fondo pixel  or  /fondo none"""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Uso: /fondo &lt;color|pixel|none&gt;\nEj: /fondo rojo, /fondo pixel, /fondo none")
        return
    val = context.args[0].lower()
    bot_config.set(chat_id, "bg", val)
    await update.message.reply_text(f"✅ Fondo por defecto: <b>{val}</b>", parse_mode="HTML")


async def cmd_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set default overlay text: /texto suscríbete  or  /texto off"""
    chat_id = update.effective_chat.id
    # Get full text after /texto command
    full = update.message.text
    m = re.match(r"/texto\s+(.+)", full, re.IGNORECASE)
    if not m:
        await update.message.reply_text("Uso: /texto &lt;frase&gt;  o  /texto off")
        return
    val = m.group(1).strip()
    if val.lower() == "off":
        bot_config.set(chat_id, "overlay_text", None)
        await update.message.reply_text("✅ Texto overlay <b>desactivado</b>", parse_mode="HTML")
    else:
        bot_config.set(chat_id, "overlay_text", val)
        await update.message.reply_text(f"✅ Texto por defecto: <b>{val[:100]}</b>", parse_mode="HTML")


async def cmd_horizontal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: set bg=none (horizontal, no letterbox)."""
    chat_id = update.effective_chat.id
    bot_config.set(chat_id, "bg", "none")
    await update.message.reply_text("✅ Modo <b>horizontal</b> activado (sin letterbox, 16:9)", parse_mode="HTML")


async def cmd_panoramico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut: set bg=pixel (9:16 panoramic)."""
    chat_id = update.effective_chat.id
    bot_config.set(chat_id, "bg", "pixel")
    await update.message.reply_text("✅ Modo <b>panorámico</b> activado (9:16, fondo pixelado)", parse_mode="HTML")


async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent jobs."""
    chat_id = update.effective_chat.id
    hist = job_queue.get_history(chat_id)
    if not hist:
        await update.message.reply_text("📭 Sin historial todavía.")
        return
    lines = ["📋 <b>Últimos trabajos</b>"]
    for h in hist[:10]:
        url_short = h["url"]
        if len(url_short) > 50:
            url_short = url_short[:47] + "..."
        status = "✅" if h["status"] == "done" else "❌"
        lines.append(f"{status} Job #{h['id']} — {h['num_clips']} clips — {h['created_at']}")
        lines.append(f"  <code>{url_short}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current queue."""
    chat_id = update.effective_chat.id
    jobs = job_queue.get_queue(chat_id)
    if not jobs:
        await update.message.reply_text("📭 Cola vacía.")
        return
    lines = ["📥 <b>Cola actual</b>"]
    for j in jobs:
        status = "🔄 procesando" if j["status"] == "processing" else "⏳ pendiente"
        lines.append(f"• Job #{j['id']} {status} — <code>{j['url'][:50]}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a pending job: /cancel <job_id>"""
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Uso: /cancel &lt;job_id&gt;\n/cancel all para cancelar todos")
        return
    arg = context.args[0].lower()
    if arg == "all":
        jobs = job_queue.get_queue(chat_id)
        cancelled = 0
        for j in jobs:
            if j["status"] == "pending" and job_queue.cancel_pending(j["id"], chat_id):
                cancelled += 1
        await update.message.reply_text(f"🗑 {cancelled} trabajos cancelados.")
        return
    try:
        job_id = int(arg)
        if job_queue.cancel_pending(job_id, chat_id):
            await update.message.reply_text(f"🗑 Job #{job_id} cancelado.")
        else:
            await update.message.reply_text(f"⚠ No se pudo cancelar job #{job_id} (no existe o ya está procesando).")
    except ValueError:
        await update.message.reply_text("ID inválido.")


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle auto-upload mode: /auto on | off"""
    chat_id = update.effective_chat.id
    if not context.args:
        current = bot_config.get(chat_id, "auto_upload", False)
        await update.message.reply_text(f"Modo automático: <b>{'ON' if current else 'OFF'}</b>", parse_mode="HTML")
        return
    arg = context.args[0].lower()
    if arg in ("on", "true", "1", "si", "sí"):
        bot_config.set(chat_id, "auto_upload", True)
        await update.message.reply_text("⚡ <b>Modo automático ON</b>\n⚠️ Los clips se publicarán en TikTok sin revisión previa.", parse_mode="HTML")
    elif arg in ("off", "false", "0", "no"):
        bot_config.set(chat_id, "auto_upload", False)
        await update.message.reply_text("🔕 Modo automático <b>OFF</b>", parse_mode="HTML")
    else:
        await update.message.reply_text("Uso: /auto on | off")


async def cmd_abtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """AB testing: /abtest on | off | results"""
    chat_id = update.effective_chat.id
    if not context.args:
        current = bot_config.get(chat_id, "ab_test", False)
        await update.message.reply_text(
            f"AB testing: <b>{'ON' if current else 'OFF'}</b>\n"
            f"Variantes: {', '.join(v['name'] for v in ab_testing.get_variants())}\n\n"
            f"Uso: /abtest on | off | results",
            parse_mode="HTML",
        )
        return
    arg = context.args[0].lower()
    if arg == "on":
        bot_config.set(chat_id, "ab_test", True)
        await update.message.reply_text("🧪 <b>AB testing ON</b> — cada clip se generará en variantes A/B.", parse_mode="HTML")
    elif arg == "off":
        bot_config.set(chat_id, "ab_test", False)
        await update.message.reply_text("🔬 AB testing <b>OFF</b>", parse_mode="HTML")
    elif arg == "results":
        results = ab_testing.get_results(limit=10)
        if not results:
            await update.message.reply_text("Sin resultados de AB tests todavía.")
            return
        lines = ["🧪 <b>Resultados AB</b>"]
        for r in results:
            lines.append(
                f"• Job #{r['job_id']} clip {r['clip_index']} "
                f"<b>{r['variant']}</b> ({r['config'][:40]}) "
                f"— views {r['views']}, likes {r['likes']}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    else:
        await update.message.reply_text("Uso: /abtest on | off | results")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset to factory defaults."""
    chat_id = update.effective_chat.id
    for k in ("num_clips", "min_clip", "max_clip", "bg", "overlay_text"):
        bot_config.set(chat_id, k, bot_config.DEFAULTS[k])
    await update.message.reply_text("🔄 Configuración <b>restablecida</b> a defaults.", parse_mode="HTML")


# ── Message handler ──────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages. Look for YouTube URLs + optional instructions.
    Enqueues job in SQLite queue; worker processes it in background."""
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if not any(d in text for d in _URL_DOMAINS):
        await update.message.reply_text(
            "👋 Envíame un enlace de YouTube, Twitch o Instagram para generar clips.\n\n"
            "Puedes añadir instrucciones después del enlace:\n"
            "https://www.youtube.com/watch?v=xxx -> horizontal, 1 clip de 30 segundos\n\n"
            "/help para ver todos los comandos."
        )
        return

    url, instructions = split_url_and_instructions(text)
    if not url:
        return

    logger.info(f"Received URL: {url}")
    logger.info(f"Instructions: {instructions!r}" if instructions else "No instructions")

    # Playlist support: expand into individual videos and enqueue all
    if is_playlist_url(url):
        try:
            urls = extract_playlist_urls(url)
        except Exception as e:
            await update.message.reply_text(f"❌ No pude leer la playlist: {str(e)[:100]}")
            return
        if not urls:
            await update.message.reply_text("⚠ Playlist vacía o sin videos accesibles.")
            return
        for u in urls:
            job_queue.enqueue(chat_id, u, instructions)
        await update.message.reply_text(
            f"📋 <b>Playlist detectada:</b> {len(urls)} videos en cola.\n"
            f"Se procesarán en orden. /queue para ver el estado.",
            parse_mode="HTML",
        )
        _ensure_worker()
        return

    job_id = job_queue.enqueue(chat_id, url, instructions)
    pending = job_queue.get_pending_count()

    await update.message.reply_text(
        f"📥 <b>En cola (job #{job_id})</b> — posición {pending}.\n"
        f"Procesando en breve...\n"
        f"📝 <i>{instructions[:150] if instructions else 'sin instrucciones'}</i>",
        parse_mode="HTML",
    )

    _ensure_worker()


# ── Callback handler (inline buttons) ────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    data = query.data
    clip_info = load_state(chat_id)

    if data.startswith("skip_"):
        clear_state(chat_id)
        await query.edit_message_caption(
            caption=query.message.caption + "\n\n⏭ _Saltado_",
            parse_mode="HTML",
        )
        return

    if data.startswith("upload_"):
        if not clip_info:
            await query.edit_message_caption(
                caption=query.message.caption + "\n\n❌ _Estado perdido. Reenvía el video._",
                parse_mode="HTML",
            )
            return

        await query.edit_message_caption(
            caption=query.message.caption + "\n\n⏳ _Subiendo a TikTok feed..._",
            parse_mode="HTML",
        )

        try:
            desc = clip_info.get("description") or "Clip panorámico"
            tags = clip_info.get("tags") or []
            upload_video(clip_info["path"], desc, tags, draft=False)
            await query.edit_message_caption(
                caption=query.message.caption.replace("⏳ _Subiendo a TikTok feed..._", "")
                + "\n\n✅ <b>Publicado en TikTok!</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            await query.edit_message_caption(
                caption=query.message.caption.replace("⏳ _Subiendo a TikTok feed..._", "")
                + f"\n\n❌ _Error: {str(e)[:100]}_",
                parse_mode="HTML",
            )
        finally:
            clear_state(chat_id)


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    """Start the bot."""
    global _application
    app = Application.builder().token(TOKEN).build()
    _application = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("clips", cmd_clips))
    app.add_handler(CommandHandler("duracion", cmd_duracion))
    app.add_handler(CommandHandler("fondo", cmd_fondo))
    app.add_handler(CommandHandler("texto", cmd_texto))
    app.add_handler(CommandHandler("horizontal", cmd_horizontal))
    app.add_handler(CommandHandler("panoramico", cmd_panoramico))
    app.add_handler(CommandHandler("historial", cmd_historial))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("abtest", cmd_abtest))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))

    # Resume any jobs stuck in 'processing' from a previous crash
    conn = job_queue._conn()
    conn.execute("UPDATE jobs SET status='pending' WHERE status='processing'")
    conn.commit()
    conn.close()

    _ensure_worker()
    logger.info("Bot started. Listening for YouTube URLs...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
