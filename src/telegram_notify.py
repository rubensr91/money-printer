"""
Telegram Notifier - MoneyPrinterV2
Sends notifications via Telegram Bot API when videos are ready.
"""

import os
import json
import requests

from config import ROOT_DIR

TELEGRAM_CONFIG = os.path.join(ROOT_DIR, ".mp", "telegram.json")


def setup_telegram(bot_token, chat_id):
    """Save Telegram credentials."""
    config = {"bot_token": bot_token, "chat_id": str(chat_id)}
    os.makedirs(os.path.dirname(TELEGRAM_CONFIG), exist_ok=True)
    with open(TELEGRAM_CONFIG, "w") as f:
        json.dump(config, f, indent=2)
    return True


def get_chat_id(bot_token):
    """
    Get chat ID by checking recent messages sent to the bot.
    User must send ANY message to the bot first.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    resp = requests.get(url, timeout=10)
    data = resp.json()

    if not data.get("ok"):
        return None

    updates = data.get("result", [])
    if not updates:
        return None

    chat_id = updates[-1].get("message", {}).get("chat", {}).get("id")
    return chat_id


def send_notification(message):
    """
    Send a Telegram message using saved credentials.
    Returns True if sent, False otherwise.
    """
    if not os.path.exists(TELEGRAM_CONFIG):
        print("[WARN] Telegram no configurado. Usa setup_telegram() primero.")
        return False

    with open(TELEGRAM_CONFIG, "r") as f:
        config = json.load(f)

    bot_token = config.get("bot_token")
    chat_id = config.get("chat_id")

    if not bot_token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"[ERROR] Telegram send failed: {e}")
        return False


def notify_video_ready(folder, clips_info):
    """
    Send a notification that clips are ready in drafts.

    Args:
        folder: Output folder path
        clips_info: List of dicts with {path, duration, desc, tags}
    """
    clip_lines = []
    for i, clip in enumerate(clips_info):
        dur = clip.get("duration", "?")
        desc = clip.get("desc", "")
        tags = " ".join(clip.get("tags", []))
        clip_lines.append(f"<b>Clip {i+1}</b> ({dur}s): {desc}")
        if tags:
            clip_lines.append(f"  {tags}")

    msg = f"🎬 <b>Videos listos en TikTok drafts</b>\n\n"
    msg += "\n".join(clip_lines)
    msg += f"\n\n📁 Carpeta: {folder}"

    return send_notification(msg)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Telegram Notifier")
    parser.add_argument("--setup", nargs=2, metavar=("TOKEN", "CHAT_ID"), help="Setup bot")
    parser.add_argument("--get-chat-id", help="Get chat ID from bot token")
    parser.add_argument("--test", help="Send test message")
    args = parser.parse_args()

    if args.setup:
        token, cid = args.setup
        setup_telegram(token, cid)
        send_notification("✅ MoneyPrinter V2 conectado a Telegram")
        print("Configurado y test enviado.")

    elif args.get_chat_id:
        cid = get_chat_id(args.get_chat_id)
        if cid:
            print(f"Chat ID encontrado: {cid}")
            print(f"Úsalo: python -m src.telegram_notify --setup {args.get_chat_id} {cid}")
        else:
            print("No se encontró chat ID. Envía un mensaje a tu bot primero.")

    elif args.test:
        ok = send_notification(args.test)
        print("Enviado" if ok else "Fallo")

    else:
        parser.print_help()
