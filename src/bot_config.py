"""
Bot Config — per-chat settings persisted as JSON.
Used by telegram_bot.py to store user preferences (clip count, duration, bg, etc.).
"""

import os
import json
import threading

from config import ROOT_DIR

CONFIG_FILE = os.path.join(ROOT_DIR, ".mp", "bot_config.json")
_lock = threading.Lock()

DEFAULTS = {
    "num_clips": 3,
    "min_clip": 20,
    "max_clip": 60,
    "bg": "pixel",
    "overlay_text": None,
    "auto_upload": False,
    "ab_test": False,
    "history": [],  # list of {url, clips, timestamp}
}

def _load():
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get(chat_id, key, default=None):
    """Get a config value for a chat. Falls back to DEFAULTS."""
    with _lock:
        cfg = _load()
        chat = cfg.get(str(chat_id), {})
        return chat.get(key, DEFAULTS.get(key, default))


def set(chat_id, key, value):
    """Set a config value for a chat."""
    with _lock:
        cfg = _load()
        cid = str(chat_id)
        if cid not in cfg:
            cfg[cid] = {}
        cfg[cid][key] = value
        _save(cfg)


def get_all(chat_id):
    """Return merged dict: defaults + user overrides."""
    with _lock:
        cfg = _load()
        chat = cfg.get(str(chat_id), {})
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in chat.items() if v is not None})
        # Don't include history in the display config
        merged.pop("history", None)
        return merged


def add_history(chat_id, url, num_clips_sent):
    """Record a completed job."""
    with _lock:
        cfg = _load()
        cid = str(chat_id)
        if cid not in cfg:
            cfg[cid] = {}
        hist = cfg[cid].get("history", [])
        import datetime
        hist.append({
            "url": url,
            "clips": num_clips_sent,
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        # Keep last 10
        if len(hist) > 10:
            hist = hist[-10:]
        cfg[cid]["history"] = hist
        _save(cfg)


def get_history(chat_id):
    """Return recent jobs for a chat."""
    with _lock:
        cfg = _load()
        return cfg.get(str(chat_id), {}).get("history", [])
