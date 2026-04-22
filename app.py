"""Flask backend.

Endpoints triggered by cron-job.org:
  GET /poll        run a poll (every 30 min, 24/7)
  GET /analyze     analyze yesterday's logical day (once daily ~9:00 Berlin)

Both can be guarded by ?token=... when CRON_TOKEN env var is set.

Both /poll and /analyze respond immediately with 200 OK and run the actual
work in a background thread. This avoids cron-job.org timeouts (30s) while
Render's free tier is waking up from sleep (can take 30-60s).

Other endpoints:
  GET  /            human-readable status page
  GET  /excel       download latest Excel
  GET  /health      liveness check
  POST /reset       wipe incidents.json (guarded by ADMIN_TOKEN)
"""

import json
import os
import threading
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


def _run_in_background(fn, *args, **kwargs):
    """Start a daemon thread running fn(*args, **kwargs). Errors are logged
    but do not crash the server."""
    def _wrapper():
        try:
            fn(*args, **kwargs)
        except Exception as e:
            import traceback
            print(f"[background] {fn.__name__} failed: {e}")
            traceback.print_exc()
    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()
    return t


# Locks to prevent overlapping runs of the same job (cron-job.org might fire
# a second trigger while the previous one is still executing, especially on
# the first wake-up from Render sleep).
_poll_lock = threading.Lock()
_analyze_lock = threading.Lock()


def _guarded_poll():
    if not _poll_lock.acquire(blocking=False):
        print("[poll] previous run still in progress, skipping")
        return
    try:
        poll_day.poll_once()
    finally:
        _poll_lock.release()


def _guarded_analyze(date):
    if not _analyze_lock.acquire(blocking=False):
        print("[analyze] previous run still in progress, skipping")
        return
    try:
        analyze_day.run(date)
        try:
            generate_excel.generate()
        except Exception as e:
            print(f"[analyze] excel generation failed: {e}")
    finally:
        _analyze_lock.release()


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
    _run_in_background(_guarded_poll)
    return jsonify({
        "status": "started",
        "message": "Poll running in background",
        "at": datetime.utcnow().isoformat(),
    })


@app.get("/analyze")
def analyze():
    err = _require_cron_token()
    if err is not None:
        return err
    date = request.args.get("date") or None
    _run_in_background(_guarded_analyze, date)
    return jsonify({
        "status": "started",
        "message": "Analysis running in background",
        "date": date or "yesterday",
        "at": datetime.utcnow().isoformat(),
    })


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


# ---- Debug endpoints (temporary, for diagnosing API behavior) -------------

@app.get("/debug/durations")
def debug_durations():
    """Test how many trips the API returns for different duration values.

    Query params:
      when  - ISO datetime (optional, defaults to now)
    """
    err = _require_cron_token()
    if err is not None:
        return err
    import debug_api
    config = poll_day.load_config()
    when = request.args.get("when") or None
    result = debug_api.test_durations(
        config["station"]["id"],
        config["products"],
        when_iso=when,
    )
    return jsonify(result)


@app.get("/debug/trip")
def debug_trip():
    """Fetch a specific trip and show its real-time data completeness.

    Query params:
      id  - trip ID (required)
    """
    err = _require_cron_token()
    if err is not None:
        return err
    import debug_api
    trip_id = request.args.get("id")
    if not trip_id:
        return jsonify({"error": "Missing id parameter"}), 400
    # Strip accidental quoting from browser copy-paste
    trip_id = trip_id.strip().strip('"').strip("'")
    try:
        return jsonify(debug_api.test_trip_completeness(trip_id))
    except Exception as e:
        return jsonify({"error": str(e), "trip_id": trip_id}), 500


@app.get("/debug/lookup")
def debug_lookup():
    """Search for stations by name. Returns ID and display name.

    Query params:
      q  - station name or fragment (required)
    """
    err = _require_cron_token()
    if err is not None:
        return err
    import requests as _req
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Missing q parameter"}), 400
    try:
        r = _req.get(
            "https://v6.db.transport.rest/locations",
            params={"query": q, "results": 10,
                    "poi": "false", "addresses": "false"},
            headers={"User-Agent": "bahn-pb/1.0"},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    out = []
    for item in r.json() or []:
        if item.get("type") != "stop":
            continue
        prods = item.get("products") or {}
        out.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "products": sorted([k for k, v in prods.items() if v]),
        })
    return jsonify({"query": q, "results": out})


@app.get("/debug/arrivals")
def debug_arrivals():
    """Show arrivals at a station (the other side of departures).

    Lets us see whether the API still reports arrival data after trains have
    arrived (crucial: if yes, we can pick up late trips even after-the-fact).

    Query params:
      id       - station id (required)
      duration - minutes to look back (default 60)
    """
    err = _require_cron_token()
    if err is not None:
        return err
    import requests as _req
    station_id = request.args.get("id")
    if not station_id:
        return jsonify({"error": "Missing id parameter"}), 400
    try:
        dur = int(request.args.get("duration", "60"))
    except ValueError:
        dur = 60

    try:
        r = _req.get(
            f"https://v6.db.transport.rest/stops/{station_id}/arrivals",
            params={"duration": dur, "results": 100},
            headers={"User-Agent": "bahn-pb/1.0"},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    arrs = body.get("arrivals") if isinstance(body, dict) else body
    arrs = arrs or []
    simplified = []
    for a in arrs[:50]:
        line = a.get("line") or {}
        simplified.append({
            "trip_id": a.get("tripId"),
            "line": line.get("name"),
            "origin": a.get("provenance") or (a.get("origin") or {}).get("name"),
            "planned_when": a.get("plannedWhen"),
            "when": a.get("when"),
            "delay_minutes": (int(a.get("delay") // 60)
                              if a.get("delay") is not None else None),
            "cancelled": bool(a.get("cancelled")),
        })
    return jsonify({"count": len(arrs), "arrivals": simplified})


# ---- Next wave of debug endpoints (profile, journeys/HYBRID, trips) -------

@app.get("/debug/profile_test")
def debug_profile_test():
    """Compare what different API 'profile' values return.

    Tries 'dbnav' (default), 'db' (bahn.de backend), 'dbweb'. For each, polls
    departures with duration=720 and reports how many trips came back and how
    large the actual time span was.
    """
    err = _require_cron_token()
    if err is not None:
        return err
    import requests as _req
    station_id = request.args.get("id")
    if not station_id:
        return jsonify({"error": "Missing id parameter"}), 400

    profiles = ["dbnav", "db", "dbweb"]
    result = {"station_id": station_id, "results": []}

    for profile in profiles:
        try:
            r = _req.get(
                f"https://v6.db.transport.rest/stops/{station_id}/departures",
                params={
                    "duration": 720,
                    "results": 1000,
                    "profile": profile,
                    "remarks": "false",
                },
                headers={"User-Agent": "bahn-pb/1.0"},
                timeout=30,
            )
            r.raise_for_status()
            body = r.json()
            deps = body.get("departures") if isinstance(body, dict) else body
            deps = deps or []

            times = [d.get("plannedWhen") for d in deps if d.get("plannedWhen")]
            times.sort()
            first, last = (times[0], times[-1]) if times else (None, None)
            span_min = None
            if first and last:
                try:
                    dt_f = datetime.fromisoformat(first.replace("Z", "+00:00"))
                    dt_l = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    span_min = int((dt_l - dt_f).total_seconds() // 60)
                except Exception:
                    pass

            cancelled_count = sum(1 for d in deps if d.get("cancelled"))

            result["results"].append({
                "profile": profile,
                "trips_returned": len(deps),
                "unique_trip_ids": len({d.get("tripId") for d in deps
                                        if d.get("tripId")}),
                "earliest_when": first,
                "latest_when": last,
                "span_minutes": span_min,
                "cancelled_trips": cancelled_count,
            })
        except Exception as e:
            result["results"].append({
                "profile": profile,
                "error": str(e),
            })
    return jsonify(result)


@app.get("/debug/journeys")
def debug_journeys():
    """Query /journeys with multiple parameter combinations to find what works.

    Tries different combinations of routingMode, transfers, profile, and
    departure time, so we can pinpoint which settings produce results and
    whether HYBRID actually surfaces cancellations.

    Query params:
      from      - station id (required)
      to        - station id (required)
      departure - ISO datetime; default: tomorrow 08:00 local
    """
    err = _require_cron_token()
    if err is not None:
        return err
    import requests as _req
    import time as _time
    from datetime import datetime as _dt, timedelta as _td
    try:
        from zoneinfo import ZoneInfo
        berlin = ZoneInfo("Europe/Berlin")
    except Exception:
        berlin = None

    from_id = request.args.get("from")
    to_id = request.args.get("to")
    if not from_id or not to_id:
        return jsonify({"error": "Missing from or to parameter"}), 400

    departure = request.args.get("departure")
    if not departure:
        now = _dt.now(berlin) if berlin else _dt.now()
        tmrw_8 = (now.replace(hour=8, minute=0, second=0, microsecond=0)
                  + _td(days=1))
        departure = tmrw_8.isoformat()

    variants = [
        {"label": "default (REALTIME)", "routingMode": None,
         "transfers": None, "profile": None},
        {"label": "HYBRID only", "routingMode": "HYBRID",
         "transfers": None, "profile": None},
        {"label": "HYBRID + transfers=0", "routingMode": "HYBRID",
         "transfers": 0, "profile": None},
        {"label": "profile=db", "routingMode": None,
         "transfers": None, "profile": "db"},
        {"label": "profile=db + HYBRID", "routingMode": "HYBRID",
         "transfers": None, "profile": "db"},
        {"label": "profile=db + HYBRID + transfers=0", "routingMode": "HYBRID",
         "transfers": 0, "profile": "db"},
    ]

    results = []
    for i, v in enumerate(variants):
        # Rate-limit: 1.5s between calls
        if i > 0:
            _time.sleep(1.5)

        params = {
            "from": from_id,
            "to": to_id,
            "results": 10,
            "departure": departure,
            "stopovers": "false",
            "remarks": "false",
        }
        if v["routingMode"]:
            params["routingMode"] = v["routingMode"]
        if v["transfers"] is not None:
            params["transfers"] = v["transfers"]
        if v["profile"]:
            params["profile"] = v["profile"]

        try:
            r = _req.get(
                "https://v6.db.transport.rest/journeys",
                params=params,
                headers={"User-Agent": "bahn-pb/1.0"},
                timeout=30,
            )
            status = r.status_code
            if status == 200:
                body = r.json()
                journeys = body.get("journeys") or []
                total_legs = 0
                cancelled_legs = 0
                direct_journeys = 0
                products_seen: dict[str, int] = {}
                lines_seen: dict[str, int] = {}
                sample = []
                for j in journeys:
                    legs = [l for l in (j.get("legs") or [])
                            if not l.get("walking")]
                    total_legs += len(legs)
                    if len(legs) == 1:
                        direct_journeys += 1
                    for l in legs:
                        line = l.get("line") or {}
                        p = line.get("product") or "?"
                        n = line.get("name") or "?"
                        products_seen[p] = products_seen.get(p, 0) + 1
                        lines_seen[n] = lines_seen.get(n, 0) + 1
                        if l.get("cancelled"):
                            cancelled_legs += 1
                        if len(sample) < 5:
                            sample.append({
                                "line": n,
                                "product": p,
                                "planned_departure": l.get("plannedDeparture"),
                                "cancelled": bool(l.get("cancelled")),
                                "legs_in_journey": len(legs),
                            })
                results.append({
                    "variant": v["label"],
                    "status": 200,
                    "journeys_returned": len(journeys),
                    "direct_journeys": direct_journeys,
                    "total_legs": total_legs,
                    "cancelled_legs": cancelled_legs,
                    "products_seen": products_seen,
                    "lines_seen": lines_seen,
                    "sample": sample,
                })
            else:
                results.append({
                    "variant": v["label"],
                    "status": status,
                    "body_sample": r.text[:200],
                })
        except Exception as e:
            results.append({"variant": v["label"], "error": str(e)})

    return jsonify({
        "departure_used": departure,
        "from": from_id,
        "to": to_id,
        "results": results,
    })


@app.get("/debug/departures_direction")
def debug_departures_direction():
    """Query departures from station A with direction=B filter.

    This may be a more reliable way to see 'all direct trains from A toward
    B', independent of the journey router's opinion about what constitutes a
    reasonable connection.

    Query params:
      from      - station id (required)
      direction - station id to filter by (required)
      when      - ISO datetime; default: tomorrow 08:00 local
    """
    err = _require_cron_token()
    if err is not None:
        return err
    import requests as _req
    from datetime import datetime as _dt, timedelta as _td
    try:
        from zoneinfo import ZoneInfo
        berlin = ZoneInfo("Europe/Berlin")
    except Exception:
        berlin = None

    from_id = request.args.get("from")
    direction_id = request.args.get("direction")
    if not from_id or not direction_id:
        return jsonify({"error": "Missing from or direction"}), 400

    when = request.args.get("when")
    if not when:
        now = _dt.now(berlin) if berlin else _dt.now()
        tmrw_8 = (now.replace(hour=8, minute=0, second=0, microsecond=0)
                  + _td(days=1))
        when = tmrw_8.isoformat()

    params = {
        "direction": direction_id,
        "duration": 60,
        "results": 100,
        "when": when,
        "profile": "db",
        "remarks": "false",
    }

    try:
        r = _req.get(
            f"https://v6.db.transport.rest/stops/{from_id}/departures",
            params=params,
            headers={"User-Agent": "bahn-pb/1.0"},
            timeout=30,
        )
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    deps = body.get("departures") if isinstance(body, dict) else body
    deps = deps or []
    simplified = []
    products_seen: dict[str, int] = {}
    lines_seen: dict[str, int] = {}
    cancelled_count = 0
    for d in deps:
        line = d.get("line") or {}
        p = line.get("product") or "?"
        n = line.get("name") or "?"
        products_seen[p] = products_seen.get(p, 0) + 1
        lines_seen[n] = lines_seen.get(n, 0) + 1
        if d.get("cancelled"):
            cancelled_count += 1
        simplified.append({
            "line": n,
            "product": p,
            "direction": d.get("direction"),
            "planned_when": d.get("plannedWhen"),
            "when": d.get("when"),
            "cancelled": bool(d.get("cancelled")),
        })
    return jsonify({
        "when_used": when,
        "total": len(deps),
        "cancelled": cancelled_count,
        "products": products_seen,
        "lines": lines_seen,
        "first_10": simplified[:10],
    })


@app.get("/debug/trips_by_name")
def debug_trips_by_name():
    """Try the (undocumented for db-rest) /trips endpoint with 'query' filter.

    This is how the VBB API exposes 'find trips by line name/number'. It may
    or may not work on the DB profile — this test will tell us.

    Query params:
      query - line name or fahrtNr (e.g. 'RE11' or '26736')
      when  - ISO datetime (optional; defaults to now)
    """
    err = _require_cron_token()
    if err is not None:
        return err
    import requests as _req

    q = request.args.get("query")
    if not q:
        return jsonify({"error": "Missing query parameter"}), 400

    attempts = []

    # Attempt 1: /trips with query
    try:
        r = _req.get(
            "https://v6.db.transport.rest/trips",
            params={"query": q},
            headers={"User-Agent": "bahn-pb/1.0"},
            timeout=30,
        )
        attempts.append({
            "endpoint": "/trips?query=",
            "status": r.status_code,
            "body_sample": r.text[:500] if r.status_code != 200 else None,
            "parsed_count": (
                len(r.json()) if r.status_code == 200
                and isinstance(r.json(), list) else None
            ),
        })
    except Exception as e:
        attempts.append({"endpoint": "/trips?query=",
                         "error": str(e)})

    # Attempt 2: /trips with lineName
    try:
        r = _req.get(
            "https://v6.db.transport.rest/trips",
            params={"lineName": q},
            headers={"User-Agent": "bahn-pb/1.0"},
            timeout=30,
        )
        attempts.append({
            "endpoint": "/trips?lineName=",
            "status": r.status_code,
            "body_sample": r.text[:500] if r.status_code != 200 else None,
            "parsed_count": (
                len(r.json()) if r.status_code == 200
                and isinstance(r.json(), list) else None
            ),
        })
    except Exception as e:
        attempts.append({"endpoint": "/trips?lineName=",
                         "error": str(e)})

    # Attempt 3: /lines?query= (if supported, returns line metadata)
    try:
        r = _req.get(
            "https://v6.db.transport.rest/lines",
            params={"query": q},
            headers={"User-Agent": "bahn-pb/1.0"},
            timeout=30,
        )
        attempts.append({
            "endpoint": "/lines?query=",
            "status": r.status_code,
            "body_sample": r.text[:500] if r.status_code != 200 else None,
        })
    except Exception as e:
        attempts.append({"endpoint": "/lines?query=",
                         "error": str(e)})

    return jsonify({"query": q, "attempts": attempts})


@app.get("/debug/departures")
def debug_departures():
    """Show the most recent departures the API returns right now (for picking
    trip IDs to test /debug/trip with)."""
    err = _require_cron_token()
    if err is not None:
        return err
    config = poll_day.load_config()
    station_id = config["station"]["id"]
    try:
        deps = poll_day.fetch_departures(
            station_id, config["products"], duration_minutes=120,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    simplified = []
    for d in deps[:50]:
        line = d.get("line") or {}
        stop = d.get("stop") or {}
        simplified.append({
            "trip_id": d.get("tripId"),
            "line": line.get("name"),
            "direction": d.get("direction"),
            "when": d.get("when"),
            "planned_when": d.get("plannedWhen"),
            "delay_minutes": (int(d.get("delay") // 60)
                              if d.get("delay") is not None else None),
            "platform": d.get("platform"),
            "station": stop.get("name"),
        })
    return jsonify({"count": len(deps), "departures": simplified})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
