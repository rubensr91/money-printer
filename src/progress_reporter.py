"""
Progress Reporter - Telegram status updates every 5 seconds.
Runs in background thread, edits same message to avoid spam.
"""

import os
import sys
import time
import json
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telegram_notify import TELEGRAM_CONFIG, send_notification


class ProgressReporter:
    """Sends Telegram progress updates every 5 seconds via message editing."""

    def __init__(self, chat_id=None, enabled=True):
        self._enabled = enabled
        self._lock = threading.Lock()
        self._stage = "Iniciando..."
        self._percent = 0
        self._detail = ""
        self._running = False
        self._thread = None
        self._message_id = None
        self._chat_id = chat_id
        self._last_text = ""

        if self._enabled and not chat_id:
            try:
                with open(TELEGRAM_CONFIG) as f:
                    cfg = json.load(f)
                self._chat_id = int(cfg["chat_id"])
            except:
                self._enabled = False

    def start(self):
        """Start the background reporter thread."""
        if not self._enabled:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, final_msg=""):
        """Stop the reporter and send final message."""
        self._running = False
        if self._enabled and final_msg:
            self._send(final_msg)

    def update(self, stage, percent, detail=""):
        """Update current progress. Called from pipeline."""
        with self._lock:
            self._stage = stage
            self._percent = min(99, max(0, int(percent)))
            self._detail = detail

    def _run(self):
        """Background loop: send update every 5 seconds."""
        import requests

        # Send initial message
        token = self._get_token()
        if not token:
            return

        initial_text = self._format_msg()
        msg_id = self._send_raw(token, self._chat_id, initial_text)

        # Edit every 5 seconds
        while self._running:
            time.sleep(5)
            with self._lock:
                text = self._format_msg()
                if text == self._last_text:
                    continue
                self._last_text = text

            if msg_id:
                self._edit_raw(token, self._chat_id, msg_id, text)

    def _format_msg(self):
        bar = self._progress_bar(self._percent)
        msg = f"{bar} {self._percent}%\n<b>{self._stage}</b>"
        if self._detail:
            msg += f"\n<i>{self._detail}</i>"
        return msg

    def _progress_bar(self, pct):
        filled = pct // 10
        empty = 10 - filled
        return "🟩" * filled + "⬜" * empty

    def _get_token(self):
        try:
            with open(TELEGRAM_CONFIG) as f:
                cfg = json.load(f)
            return cfg.get("bot_token")
        except:
            return None

    def _send_raw(self, token, chat_id, text):
        import requests
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            data = r.json()
            return data.get("result", {}).get("message_id") if data.get("ok") else None
        except:
            return None

    def _edit_raw(self, token, chat_id, msg_id, text):
        import requests
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/editMessageText",
                json={"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except:
            pass

    def _send(self, text):
        """Send a standalone notification."""
        if not self._enabled:
            return
        token = self._get_token()
        if token:
            self._send_raw(token, self._chat_id, text)
