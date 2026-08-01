"""
AB Testing — generates clip variants for A/B comparison and tracks results.
Variants differ in background/overlay. Results are recorded in SQLite
(jobs.db, ab_tests table). Metric comparison requires TikTok data; without
the official API, results are recorded as posted and can be filled later
via /abtest results.

Flow:
  1. For a job, render each viral moment in N variants.
  2. Upload all variants (in auto mode) or send them for manual review.
  3. Record ab_tests rows with variant info.
"""

import os
import json
import sqlite3
import datetime

from config import ROOT_DIR

DB_FILE = os.path.join(ROOT_DIR, ".mp", "jobs.db")

# Default variants: control + one alternative
VARIANTS = [
    {"name": "A", "bg": "pixel", "overlay_text": None},
    {"name": "B", "bg": "white", "overlay_text": "suscríbete"},
]


def _conn():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ab_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER,
            clip_index INTEGER,
            variant TEXT,
            config TEXT,
            path TEXT,
            status TEXT DEFAULT 'posted',
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            posted_at TEXT
        )
    """)
    conn.commit()
    return conn


def record_test(job_id, clip_index, variant_name, variant_config, path):
    """Record a posted variant."""
    conn = _conn()
    conn.execute(
        "INSERT INTO ab_tests (job_id, clip_index, variant, config, path, posted_at) VALUES (?,?,?,?,?,?)",
        (job_id, clip_index, variant_name, json.dumps(variant_config), path,
         datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def get_results(job_id=None, limit=20):
    """Return recorded AB tests, newest first."""
    conn = _conn()
    if job_id is not None:
        rows = conn.execute(
            "SELECT * FROM ab_tests WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM ab_tests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_variants():
    """Return current variant list."""
    return VARIANTS
