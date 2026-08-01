"""
MoneyPrinter Telegram Bot - reactive bot via python-telegram-bot.
Receives YouTube URLs, generates clips streaming, sends them one by one.
Inline buttons: [Subir a TikTok] [Saltar]
"""

import os
import sys
import json
import re
import asyncio
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from telegram_notify import TELEGRAM_CONFIG
from tiktok_clips import main_stream
from tiktok_uploader import upload_video
from progress_reporter import ProgressReporter
from config import ROOT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(ROOT_DIR, ".mp", "bot_state.json")

with open(TELEGRAM_CONFIG) as f:
    cfg = json.load(f)
TOKEN = cfg["bot_token"]


def save_state(chat_id, clip_info):
    """Save pending clip info for callback retrieval."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    state[str(chat_id)] = clip_info
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_state(chat_id):
    """Load pending clip info."""
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE) as f:
        state = json.load(f)
    return state.get(str(chat_id))


def clear_state(chat_id):
    """Clear pending clip info."""
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
    # Remove ALL URLs from text, leaving only the instructions
    instructions = text
    for u in urls:
        instructions = instructions.replace(u, "")
    instructions = re.sub(r"\s+", " ", instructions).strip()
    return url, instructions


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages. Look for YouTube URLs + optional instructions."""
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "youtube.com/watch" not in text and "youtu.be/" not in text:
        await update.message.reply_text(
            "👋 Envíame un enlace de YouTube para generar clips.\n\n"
            "Puedes añadir instrucciones después del enlace:\n"
            "https://www.youtube.com/watch?v=xxx -> céntrate en los momentos más divertidos, clips de 30s\n\n"
            "Ejemplo: https://www.youtube.com/watch?v=xxx"
        )
        return

    url, instructions = split_url_and_instructions(text)
    if not url:
        return

    logger.info(f"Received URL: {url}")
    logger.info(f"Instructions: {instructions!r}" if instructions else "No instructions")

    reporter = ProgressReporter(chat_id=chat_id)
    reporter.start()
    if instructions:
        msg = await update.message.reply_text(
            f"⏳ Iniciando pipeline...\n📝 <i>Instrucciones recibidas: \"{instructions[:200]}\"</i>",
            parse_mode="HTML",
        )
    else:
        msg = await update.message.reply_text("⏳ Iniciando pipeline...")

    try:
        clip_count = 0
        for clip in main_stream(url, min_clip=20, max_clip=60, num_clips=3,
                                reporter=reporter, instructions=instructions or None):
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

            # Save state for callback
            save_state(chat_id, {
                "path": path,
                "clip_idx": clip_count,
            })

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

            # Update progress message
            await msg.edit_text(f"✅ Clip {clip_count} enviado. Generando siguiente...")

        if clip_count == 0:
            await msg.edit_text("⚠ No se encontraron momentos para generar clips.")
        else:
            await msg.edit_text(f"🏁 {clip_count} clips generados. Revisa cada uno arriba.")

        reporter.stop()

    except Exception as e:
        logger.error(f"Error processing {url}: {e}")
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")
        reporter.stop()


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
            upload_video(
                clip_info["path"],
                "Clip panorámico",
                [],
                draft=False,
            )
            await query.edit_message_caption(
                caption=query.message.caption.replace("⏳ _Subiendo a TikTok feed..._", "") + "\n\n✅ <b>Publicado en TikTok!</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            await query.edit_message_caption(
                caption=query.message.caption.replace("⏳ _Subiendo a TikTok feed..._", "") + f"\n\n❌ _Error: {str(e)[:100]}_",
                parse_mode="HTML",
            )
        finally:
            clear_state(chat_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 <b>MoneyPrinter V2 Bot</b>\n\n"
        "Envíame un enlace de YouTube y te generaré clips.\n\n"
        "Por defecto: formato panorámico 9:16 (fondo del mismo video).\n"
        "Añade instrucciones para personalizar:\n"
        "• <code>horizontal</code> — clip en 16:9 sin fondo\n"
        "• <code>fondo blanco</code> o <code>fondo negro</code>\n"
        "• <code>texto \"tu frase\"</code> — incrusta texto abajo\n"
        "• <code>1 clip de 30 segundos</code> — número y duración\n\n"
        "Envía /help para más info.",
        parse_mode="HTML",
    )


def main():
    """Start the bot."""
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))

    logger.info("Bot started. Listening for YouTube URLs...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
