"""
Job Queue — persistent SQLite-backed queue for Telegram clip jobs.
Survives bot restarts. Jobs: pending -> processing -> done | failed.
"""

import os
import json
import sqlite3
import datetime
import threading

from config import ROOT_DIR

DB_FILE = os.path.join(ROOT_DIR, ".mp", "jobs.db")
_lock = threading.Lock()


def _conn():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            url TEXT NOT NULL,
            instructions TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            finished_at TEXT,
            error TEXT,
            num_clips INTEGER DEFAULT 0,
            output_paths TEXT DEFAULT '[]',
            dest_tiktok INTEGER DEFAULT 0
        )
    """)
    # Migration: add dest_tiktok column if missing (older DBs)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
    if "dest_tiktok" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN dest_tiktok INTEGER DEFAULT 0")
    conn.commit()
    return conn


def enqueue(chat_id, url, instructions="", dest_tiktok=False):
    """Add a job to the queue. Returns job id.
    dest_tiktok=True -> processed video goes to TikTok draft instead of Telegram."""
    with _lock:
        conn = _conn()
        cur = conn.execute(
            "INSERT INTO jobs (chat_id, url, instructions, status, created_at, dest_tiktok) VALUES (?,?,?,?,?,?)",
            (str(chat_id), url, instructions or "", "pending",
             datetime.datetime.now().isoformat(timespec="seconds"), int(bool(dest_tiktok))),
        )
        conn.commit()
        job_id = cur.lastrowid
        conn.close()
    return job_id


def dequeue():
    """Get next pending job (oldest first), mark it processing atomically."""
    with _lock:
        conn = _conn()
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            conn.close()
            return None
        conn.execute("UPDATE jobs SET status='processing' WHERE id=?", (row["id"],))
        conn.commit()
        job = dict(row)
        job["status"] = "processing"
        conn.close()
    return job


def update_status(job_id, status, error=None, num_clips=None, output_paths=None):
    """Update job status."""
    with _lock:
        conn = _conn()
        fields = ["status=?", "finished_at=?"]
        vals = [status, datetime.datetime.now().isoformat(timespec="seconds")]
        if error is not None:
            fields.append("error=?")
            vals.append(str(error)[:500])
        if num_clips is not None:
            fields.append("num_clips=?")
            vals.append(int(num_clips))
        if output_paths is not None:
            fields.append("output_paths=?")
            vals.append(json.dumps(output_paths))
        vals.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", vals)
        conn.commit()
        conn.close()


def get_queue(chat_id):
    """List pending + processing jobs for a chat."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE chat_id=? AND status IN ('pending','processing') ORDER BY id ASC",
        (str(chat_id),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_pending_count():
    """Total pending jobs across all chats (for worker loop)."""
    conn = _conn()
    row = conn.execute("SELECT COUNT(*) c FROM jobs WHERE status='pending'").fetchone()
    conn.close()
    return row["c"]


def get_history(chat_id, limit=10):
    """Recent done/failed jobs for a chat."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE chat_id=? AND status IN ('done','failed') ORDER BY id DESC LIMIT ?",
        (str(chat_id), limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cancel_pending(job_id, chat_id=None):
    """Cancel a pending job. Returns True if cancelled."""
    with _lock:
        conn = _conn()
        if chat_id:
            cur = conn.execute(
                "UPDATE jobs SET status='failed', error='cancelled' WHERE id=? AND chat_id=? AND status='pending'",
                (job_id, str(chat_id)),
            )
        else:
            cur = conn.execute(
                "UPDATE jobs SET status='failed', error='cancelled' WHERE id=? AND status='pending'",
                (job_id,),
            )
        conn.commit()
        conn.close()
    return cur.rowcount > 0
