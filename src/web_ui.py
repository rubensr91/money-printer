"""
Web UI — local dashboard for MoneyPrinterV2.
Flask + htmx. Routes:
  GET  /              dashboard (queue, history, stats)
  GET  /api/queue     JSON queue
  GET  /api/history   JSON history
  POST /api/cancel/<job_id>  cancel job
  GET  /logs          tail of bot_output.log
Runs on 127.0.0.1:5050 (localhost only).
"""

import os
import json

from flask import Flask, jsonify, render_template_string, request

from config import ROOT_DIR
import job_queue

app = Flask(__name__)
LOG_FILE = os.path.join(ROOT_DIR, "bot_output.log")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>MoneyPrinter V2</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<style>
  body { font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 0; padding: 20px; }
  h1 { font-size: 20px; }
  .card { background: #1c1c1c; border: 1px solid #333; border-radius: 8px; padding: 14px; margin-bottom: 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #2a2a2a; }
  .ok { color: #4caf50; } .fail { color: #f44336; } .pend { color: #ffc107; }
  .row { display: flex; gap: 24px; flex-wrap: wrap; }
  .col { flex: 1; min-width: 300px; }
  button { background: #e0245e; color: white; border: 0; border-radius: 4px; padding: 4px 10px; cursor: pointer; }
  code { background: #222; padding: 1px 5px; border-radius: 3px; }
  pre { font-size: 12px; max-height: 300px; overflow: auto; background: #0d0d0d; padding: 10px; border-radius: 6px; }
</style>
</head>
<body>
<h1>🎬 MoneyPrinter V2 — Dashboard</h1>
<div class="row">
  <div class="col">
    <div class="card" hx-get="/api/queue" hx-trigger="every 3s" hx-swap="innerHTML">
      <h2>📥 Cola</h2>
      <div id="queue">Cargando...</div>
    </div>
    <div class="card">
      <h2>📜 Logs</h2>
      <pre hx-get="/logs" hx-trigger="every 5s" hx-swap="innerHTML">Cargando...</pre>
    </div>
  </div>
  <div class="col">
    <div class="card" hx-get="/api/history" hx-trigger="every 5s" hx-swap="innerHTML">
      <h2>📋 Historial</h2>
      <div id="history">Cargando...</div>
    </div>
  </div>
</div>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


def _queue_table(jobs):
    if not jobs:
        return "<p>Cola vacía.</p>"
    rows = []
    for j in jobs:
        status = {"pending": ("⏳ pendiente", "pend"), "processing": ("🔄 procesando", "ok")}.get(
            j["status"], (j["status"], ""))
        rows.append(
            f"<tr><td>#{j['id']}</td>"
            f"<td class='{status[1]}'>{status[0]}</td>"
            f"<td><code>{j['url'][:50]}</code></td>"
            f"<td>{j['created_at']}</td>"
            f"<td>{'<button hx-post=/api/cancel/' + str(j['id']) + ' hx-swap=none>✕</button>' if j['status']=='pending' else ''}</td></tr>"
        )
    return f"<table><tr><th>ID</th><th>Estado</th><th>URL</th><th>Creado</th><th></th></tr>{''.join(rows)}</table>"


@app.route("/api/queue")
def api_queue():
    jobs = job_queue._conn().execute(
        "SELECT * FROM jobs WHERE status IN ('pending','processing') ORDER BY id ASC"
    ).fetchall()
    rows = [dict(r) for r in jobs]
    job_queue._conn().close()
    return _queue_table(rows)


def _history_table(hist):
    if not hist:
        return "<p>Sin historial.</p>"
    rows = []
    for j in hist:
        status = "✅" if j["status"] == "done" else "❌"
        rows.append(
            f"<tr><td>{status}</td><td>#{j['id']}</td>"
            f"<td>{j['num_clips']} clips</td>"
            f"<td><code>{j['url'][:50]}</code></td>"
            f"<td>{j['created_at']}</td></tr>"
        )
    return f"<table><tr><th></th><th>ID</th><th>Clips</th><th>URL</th><th>Fecha</th></tr>{''.join(rows)}</table>"


@app.route("/api/history")
def api_history():
    conn = job_queue._conn()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status IN ('done','failed') ORDER BY id DESC LIMIT 15"
    ).fetchall()
    conn.close()
    return _history_table([dict(r) for r in rows])


@app.route("/api/cancel/<int:job_id>", methods=["POST"])
def api_cancel(job_id):
    ok = job_queue.cancel_pending(job_id)
    return jsonify({"cancelled": ok})


@app.route("/logs")
def logs():
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-100:])
    except Exception:
        return "<p>Log no disponible.</p>"


def start_web_ui():
    """Start the web UI in a background thread."""
    import threading
    t = threading.Thread(target=lambda: app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False), daemon=True)
    t.start()
    print("[INFO] Web UI en http://127.0.0.1:5050")


if __name__ == "__main__":
    start_web_ui()
    import time
    while True:
        time.sleep(10)
