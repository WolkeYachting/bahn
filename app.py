"""Flask backend.

Endpoints triggered by cron-job.org:
  GET /poll        run a poll (every 30 min, 24/7)
  GET /analyze     analyze yesterday's logical day (once daily ~9:00 Berlin)

Both can be guarded by ?token=... when CRON_TOKEN env var is set.

Other endpoints:
  GET  /            human-readable status page
  GET  /excel       download latest Excel
  GET  /health      liveness check
  POST /reset       wipe incidents.json (guarded by ADMIN_TOKEN)
"""

import json
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file, render_template_string

import poll_day
import analyze_day
import generate_excel
from storage import Storage
from timeutil import (
    current_logical_day, previous_logical_day, BERLIN, parse_iso,
)

app = Flask(__name__)
BASE = Path(__file__).parent
_storage = Storage()

CRON_TOKEN = os.environ.get("CRON_TOKEN")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")


def _require_cron_token():
    if not CRON_TOKEN:
        return None
    provided = request.args.get("token") or request.headers.get("X-Cron-Token")
    if provided != CRON_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>Bahn-Verspätungstracker Paderborn</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 20px;color:#222}
    h1{margin-bottom:4px}
    .muted{color:#666;font-size:.9em}
    .card{background:#f5f7fa;border-radius:8px;padding:16px;margin:16px 0}
    .kpi{display:inline-block;margin-right:24px}
    .kpi b{display:block;font-size:1.4em}
    a.btn{display:inline-block;padding:8px 14px;background:#305496;color:#fff;border-radius:4px;text-decoration:none;margin-right:8px}
    code{background:#eee;padding:2px 4px;border-radius:3px;word-break:break-all}
    pre{background:#eee;padding:8px;border-radius:4px;overflow-x:auto;font-size:.85em}
  </style>
</head>
<body>
  <h1>Bahn-Verspätungstracker Paderborn</h1>
  <div class="muted">Quelle: v6.db.transport.rest · Schwelle: ≥ {{ threshold }} min · Tagesgrenze: {{ boundary }}</div>

  <div class="card">
    <div class="kpi"><b>{{ total_incidents }}</b>Vorfälle</div>
    <div class="kpi"><b>{{ analyzed_days }}</b>ausgewertete Tage</div>
    <div class="kpi"><b>{{ active_today }}</b>aktive Trips heute</div>
    <div class="kpi"><b>{{ finished_today }}</b>abgeschlossen heute</div>
  </div>

  <p class="muted">Letzter Poll: {{ last_poll or '—' }}</p>

  <p>
    <a class="btn" href="/excel">Excel herunterladen</a>
  </p>

  <div class="card">
    <b>Cron-Endpunkte</b> <span class="muted">(für cron-job.org)</span>
    <pre>GET /poll{{ token_suffix }}     (alle 30 min, 24/7)
GET /analyze{{ token_suffix }}  (täglich 09:00 Europe/Berlin)</pre>
  </div>
</body>
</html>
"""


@app.get("/")
def index():
    config = poll_day.load_config()
    bh, bm = config["day_boundary_hour"], config["day_boundary_minute"]
    incidents_store = analyze_day.load_incidents()

    today = current_logical_day(bh, bm)
    today_log = poll_day.load_day_log(today)
    trips = today_log.get("trips", {}) or {}
    active = sum(1 for t in trips.values() if t.get("status") == "active")
    finished = sum(1 for t in trips.values() if t.get("status") == "finished")

    last_poll = None
    polls = today_log.get("polls") or []
    if polls:
        last_poll = polls[-1].get("started_at")
    else:
        # Fallback to yesterday
        prev = previous_logical_day(bh, bm)
        prev_log = poll_day.load_day_log(prev)
        prev_polls = prev_log.get("polls") or []
        if prev_polls:
            last_poll = prev_polls[-1].get("started_at")

    token_suffix = "?token=***" if CRON_TOKEN else ""

    return render_template_string(
        INDEX_HTML,
        total_incidents=len(incidents_store.get("incidents", [])),
        analyzed_days=len(incidents_store.get("analyzed_dates", [])),
        active_today=active,
        finished_today=finished,
        threshold=config["delay_threshold_minutes"],
        boundary=f"{bh:02d}:{bm:02d}",
        last_poll=last_poll,
        token_suffix=token_suffix,
    )


@app.get("/poll")
def poll():
    err = _require_cron_token()
    if err is not None:
        return err
    stats = poll_day.poll_once()
    return jsonify(stats)


@app.get("/analyze")
def analyze():
    err = _require_cron_token()
    if err is not None:
        return err
    date = request.args.get("date") or None
    summary = analyze_day.run(date)
    try:
        generate_excel.generate()
    except Exception as e:
        summary["excel_error"] = str(e)
    return jsonify(summary)


@app.get("/excel")
def excel():
    try:
        generate_excel.generate()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    path = BASE / generate_excel.EXPORT_FILE
    if not path.exists():
        return jsonify({"error": "Excel not available"}), 404
    filename = f"bahn_verspaetungen_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/reset")
def reset():
    if request.args.get("confirm") != "yes":
        return jsonify({"error": "Pass ?confirm=yes to reset"}), 400
    if ADMIN_TOKEN and request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    analyze_day.save_incidents(
        {"incidents": [], "diagnostics": [], "analyzed_dates": []},
        commit_message="reset: wipe incidents",
    )
    return jsonify({"status": "reset", "at": datetime.utcnow().isoformat()})


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.utcnow().isoformat(),
        "storage": {
            "github_enabled": _storage.enabled,
            "repo": _storage.repo if _storage.enabled else None,
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
