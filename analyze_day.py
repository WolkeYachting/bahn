"""Daily analysis.

Triggered by cron-job.org once per day (9:00 Europe/Berlin). Analyzes the
previous logical day:

1. Load yesterday's day-log
2. Force-finish any trips still 'active' (mark force_finished=True)
3. For each finished trip: classify as on_time / delayed / partial_cancellation
   / full_cancellation
4. Trips that never moved past 'scheduled' (never seen in live data) go to a
   diagnostic list, not the main incidents
5. Append qualifying incidents to incidents.json
6. Mark the day-log as closed
7. Regenerate the Excel
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from storage import Storage
from timeutil import (
    BERLIN, parse_iso, current_logical_day, previous_logical_day,
)

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
INCIDENTS_FILE = "incidents.json"

_storage = Storage()


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_incidents() -> dict:
    _storage.hydrate(INCIDENTS_FILE)
    raw = _storage.read(INCIDENTS_FILE)
    if not raw:
        return {
            "incidents": [],
            "diagnostics": [],
            "analyzed_dates": [],
        }
    store = json.loads(raw)
    store.setdefault("diagnostics", [])
    return store


def save_incidents(data: dict, commit_message: str) -> None:
    _storage.write(
        INCIDENTS_FILE,
        json.dumps(data, ensure_ascii=False, indent=2),
        commit_message=commit_message,
    )


def load_day_log(date: str) -> dict | None:
    filename = f"logs/{date}.json"
    _storage.hydrate(filename)
    raw = _storage.read(filename)
    return json.loads(raw) if raw else None


def save_day_log(log: dict, commit_message: str) -> None:
    filename = f"logs/{log['date']}.json"
    _storage.write(
        filename,
        json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True),
        commit_message=commit_message,
    )


def classify_trip(trip_data: dict, threshold: int) -> dict | None:
    """Classify a trip's data into delayed / partial / full cancellation, or
    None if everything was fine."""
    idx = trip_data.get("station_stopover_index")
    if idx is None:
        return None
    stopovers = trip_data.get("stopovers") or []
    downstream = stopovers[idx:]
    start_stop = downstream[0] if downstream else None
    if not start_stop:
        return None

    # Full cancellation: trip-level cancelled flag and start stop cancelled
    if trip_data.get("cancelled") and start_stop.get("cancelled"):
        return {
            "category": "full_cancellation",
            "trip_id": trip_data.get("trip_id"),
            "line": trip_data.get("line_name"),
            "direction": trip_data.get("direction"),
            "start_station": start_stop.get("stop_name"),
            "start_planned_departure": start_stop.get("planned_departure"),
            "end_station": None,
            "end_planned_arrival": None,
            "end_actual_arrival": None,
            "delay_minutes": None,
            "cancelled_from": start_stop.get("stop_name"),
            "note": "Zug nicht gestartet",
        }

    # Partial cancellation: a later stop is cancelled
    cancelled_from = None
    last_reached = start_stop
    for sp in downstream[1:]:
        if sp.get("cancelled"):
            cancelled_from = sp.get("stop_name")
            break
        last_reached = sp

    if cancelled_from:
        return {
            "category": "partial_cancellation",
            "trip_id": trip_data.get("trip_id"),
            "line": trip_data.get("line_name"),
            "direction": trip_data.get("direction"),
            "start_station": start_stop.get("stop_name"),
            "start_planned_departure": start_stop.get("planned_departure"),
            "end_station": last_reached.get("stop_name"),
            "end_planned_arrival": last_reached.get("planned_arrival"),
            "end_actual_arrival": last_reached.get("arrival"),
            "delay_minutes": last_reached.get("arrival_delay_minutes"),
            "cancelled_from": cancelled_from,
            "note": f"Ausfall ab {cancelled_from}",
        }

    # Delay: find downstream stop with confirmed (actual) arrival ≥ threshold
    best = None
    for sp in downstream[1:]:
        delay = sp.get("arrival_delay_minutes")
        if delay is None or delay < threshold:
            continue
        if not sp.get("arrival"):
            # No actual arrival recorded → still a prognosis, not confirmed
            continue
        if best is None or delay > best["arrival_delay_minutes"]:
            best = sp

    if best is None:
        return None

    return {
        "category": "delayed",
        "trip_id": trip_data.get("trip_id"),
        "line": trip_data.get("line_name"),
        "direction": trip_data.get("direction"),
        "start_station": start_stop.get("stop_name"),
        "start_planned_departure": start_stop.get("planned_departure"),
        "end_station": best.get("stop_name"),
        "end_planned_arrival": best.get("planned_arrival"),
        "end_actual_arrival": best.get("arrival"),
        "delay_minutes": best.get("arrival_delay_minutes"),
        "cancelled_from": None,
        "note": "",
    }


def force_finish_active_trips(log: dict, now_iso: str) -> int:
    """Mark all still-active trips as finished with force_finished=True."""
    forced = 0
    for trip_id, t in log.get("trips", {}).items():
        if t.get("status") == "active":
            t["status"] = "finished"
            t["finished_at"] = now_iso
            t["force_finished"] = True
            forced += 1
    return forced


def analyze_day(date: str) -> dict:
    """Returns dict with 'incidents', 'diagnostics', 'force_finished_count'."""
    config = load_config()
    threshold = config["delay_threshold_minutes"]

    log = load_day_log(date)
    if log is None:
        return {
            "date": date,
            "skipped_reason": "no_log",
            "incidents": [],
            "diagnostics": [],
            "force_finished_count": 0,
        }

    if log.get("closed"):
        return {
            "date": date,
            "skipped_reason": "already_closed",
            "incidents": [],
            "diagnostics": [],
            "force_finished_count": 0,
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    forced = force_finish_active_trips(log, now_iso)

    incidents = []
    diagnostics = []

    for trip_id, t in log.get("trips", {}).items():
        status = t.get("status")
        data = t.get("data") or {}

        if status == "scheduled":
            # Never seen in live data
            diagnostics.append({
                "date": date,
                "trip_id": trip_id,
                "line": data.get("line_name"),
                "direction": data.get("direction"),
                "reason": "never_appeared_in_live_data",
            })
            continue

        if status != "finished":
            # Defensive: shouldn't happen after force-finish above
            diagnostics.append({
                "date": date,
                "trip_id": trip_id,
                "line": data.get("line_name"),
                "direction": data.get("direction"),
                "reason": f"unexpected_status_{status}",
            })
            continue

        incident = classify_trip(data, threshold)
        if incident is None:
            continue
        incident["date"] = date
        incident["force_finished"] = bool(t.get("force_finished"))
        incidents.append(incident)

    # Mark the log as closed
    log["closed"] = True
    log["closed_at"] = now_iso
    log["force_finished_count"] = forced
    save_day_log(log, commit_message=f"close {date}: {len(incidents)} incidents")

    return {
        "date": date,
        "skipped_reason": None,
        "incidents": incidents,
        "diagnostics": diagnostics,
        "force_finished_count": forced,
    }


def run(date: str | None = None) -> dict:
    config = load_config()
    bh, bm = config["day_boundary_hour"], config["day_boundary_minute"]
    target = date or previous_logical_day(bh, bm)

    store = load_incidents()
    if target in store.get("analyzed_dates", []):
        print(f"[analyze] {target} already analyzed, skipping")
        return {"date": target, "skipped": True}

    result = analyze_day(target)
    if result.get("skipped_reason"):
        print(f"[analyze] {target} skipped: {result['skipped_reason']}")
        return {"date": target, "skipped": True,
                "reason": result["skipped_reason"]}

    store["incidents"].extend(result["incidents"])
    store["diagnostics"].extend(result["diagnostics"])
    store["analyzed_dates"] = sorted(set(
        store.get("analyzed_dates", []) + [target]
    ))
    # Keep incidents sorted: newest date first, biggest delay first
    store["incidents"].sort(
        key=lambda i: (i.get("date") or "", -(i.get("delay_minutes") or 0)),
        reverse=True,
    )

    summary = {
        "date": target,
        "skipped": False,
        "incidents_added": len(result["incidents"]),
        "diagnostics_added": len(result["diagnostics"]),
        "force_finished_count": result["force_finished_count"],
        "by_category": {},
    }
    for inc in result["incidents"]:
        c = inc["category"]
        summary["by_category"][c] = summary["by_category"].get(c, 0) + 1

    save_incidents(
        store,
        commit_message=(
            f"analyze {target}: +{len(result['incidents'])} incidents, "
            f"+{len(result['diagnostics'])} diagnostics, "
            f"{result['force_finished_count']} force-finished"
        ),
    )

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
