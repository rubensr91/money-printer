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
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from telegram_notify import TELEGRAM_CONFIG
from tiktok_clips import main_stream
from tiktok_uploader import upload_video
from progress_reporter import ProgressReporter
from config import ROOT_DIR
import bot_config

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


def split_url_and_instructions(text):
    """Split message into (url, instructions). URL = first YouTube link,
    instructions = everything else in the message."""
    urls = [w for w in text.split() if "youtube.com/watch" in w or "youtu.be/" in w]
    if not urls:
        return None, text
    url = urls[0]
    instructions = text
    for u in urls:
        instructions = instructions.replace(u, "")
    instructions = re.sub(r"\s+", " ", instructions).strip()
    return url, instructions


# ── Command handlers ─────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands."""
    await update.message.reply_text(
        "🎬 <b>MoneyPrinter V2 — Comandos</b>\n\n"
        "<b>Producción:</b>\n"
        "Envía un enlace de YouTube + instrucciones opcionales\n\n"
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
        "/historial — Últimos trabajos\n"
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
    hist = bot_config.get_history(chat_id)
    if not hist:
        await update.message.reply_text("📭 Sin historial todavía.")
        return
    lines = ["📋 <b>Últimos trabajos</b>"]
    for h in hist[-10:]:
        url_short = h["url"]
        if len(url_short) > 50:
            url_short = url_short[:47] + "..."
        lines.append(f"• {h['clips']} clips — {h['time']}")
        lines.append(f"  <code>{url_short}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset to factory defaults."""
    chat_id = update.effective_chat.id
    for k in ("num_clips", "min_clip", "max_clip", "bg", "overlay_text"):
        bot_config.set(chat_id, k, bot_config.DEFAULTS[k])
    await update.message.reply_text("🔄 Configuración <b>restablecida</b> a defaults.", parse_mode="HTML")


# ── Message handler ──────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages. Look for YouTube URLs + optional instructions.
    Uses stored config as defaults; inline instructions override stored config."""
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "youtube.com/watch" not in text and "youtu.be/" not in text:
        await update.message.reply_text(
            "👋 Envíame un enlace de YouTube para generar clips.\n\n"
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

    cfg = bot_config.get_all(chat_id)

    reporter = ProgressReporter(chat_id=chat_id)
    reporter.start()
    if instructions:
        msg = await update.message.reply_text(
            f"⏳ Iniciando pipeline...\n📝 <i>Instrucciones: \"{instructions[:200]}\"</i>",
            parse_mode="HTML",
        )
    else:
        msg = await update.message.reply_text("⏳ Iniciando pipeline...")

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

            if is_horizontal:
                caption = f"🎬 <b>Clip {clip_count}</b> ({dur:.0f}s) horizontal"
                vid_dim = {}
            else:
                caption = f"🎬 <b>Clip {clip_count}</b> ({dur:.0f}s) panorámico"
                vid_dim = {"width": 1080, "height": 1920}

            save_state(chat_id, {"path": path, "clip_idx": clip_count})

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("▶ Subir a TikTok", callback_data=f"upload_{clip_count}"),
                    InlineKeyboardButton("⏭ Saltar", callback_data=f"skip_{clip_count}"),
                ]
            ])

            with open(path, "rb") as video_file:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=video_file,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    supports_streaming=True,
                    **vid_dim,
                )

            await msg.edit_text(f"✅ Clip {clip_count} enviado. Generando siguiente...")

        if clip_count == 0:
            await msg.edit_text("⚠ No se encontraron momentos para generar clips.")
        else:
            await msg.edit_text(f"🏁 {clip_count} clips generados. Revisa cada uno arriba.")

        reporter.stop()
        bot_config.add_history(chat_id, url, clip_count)

    except Exception as e:
        logger.error(f"Error processing {url}: {e}")
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
        reporter.stop()


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
            upload_video(clip_info["path"], "Clip panorámico", [], draft=False)
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
    app = Application.builder().token(TOKEN).build()

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
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))

    logger.info("Bot started. Listening for YouTube URLs...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
