"""
MoneyPrinter Telegram Bot - reactive bot via python-telegram-bot.
Receives YouTube URLs, generates clips streaming, sends them one by one.
Inline buttons: [Subir a TikTok] [Saltar]
"""

import os
import sys
import json
import asyncio
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from telegram_notify import TELEGRAM_CONFIG
from tiktok_clips import main_stream
from tiktok_uploader import upload_video
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


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages. Look for YouTube URLs."""
    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "youtube.com/watch" not in text and "youtu.be/" not in text:
        await update.message.reply_text(
            "👋 Envíame un enlace de YouTube para generar clips.\n\nEjemplo: https://www.youtube.com/watch?v=xxx"
        )
        return

    urls = [w for w in text.split() if "youtube.com/watch" in w or "youtu.be/" in w]
    if not urls:
        return

    url = urls[0]
    msg = await update.message.reply_text("⏳ Descargando video y analizando con DeepSeek...")

    try:
        clip_count = 0
        for clip in main_stream(url, min_clip=20, max_clip=55, num_clips=4):
            clip_count += 1
            dur = clip.get("duration", 0)
            desc = clip.get("desc", "")
            tags = clip.get("tags", [])
            tag_str = " ".join(f"#{t}" for t in tags)
            path = clip["path"]

            caption = f"<b>Clip {clip_count}</b> ({dur:.0f}s)\n{desc}\n{tag_str}"

            # Save state for callback
            save_state(chat_id, {
                "path": path,
                "desc": desc,
                "tags": tags,
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
                    width=1080,
                    height=1920,
                )

            # Update progress message
            await msg.edit_text(f"✅ Clip {clip_count} enviado. Generando siguiente...")

        if clip_count == 0:
            await msg.edit_text("⚠ No se encontraron momentos para generar clips.")
        else:
            await msg.edit_text(f"🏁 {clip_count} clips generados. Revisa cada uno arriba.")

    except Exception as e:
        logger.error(f"Error processing {url}: {e}")
        await msg.edit_text(f"❌ Error: {str(e)[:200]}")


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
                clip_info["desc"],
                clip_info["tags"],
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
        "Envíame un enlace de YouTube y te generaré clips para TikTok.\n\n"
        "Cada clip vendrá con:\n"
        "• Subtítulos estilo TikTok\n"
        "• Face tracking\n"
        "• Descripción + 5 hashtags\n"
        "• Botones para subir o saltar\n\n"
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
