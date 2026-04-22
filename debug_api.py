"""Debug helpers — isolated so they don't pollute the main code paths.

These functions are called from /debug/* endpoints in app.py to answer:
  1. How many trips does the API actually return for various durations?
  2. Are trip data complete (i.e., do past stopovers have 'arrival' filled in
     so we could classify a trip even if we only saw it once near the end)?
  3. How long after planned departure does a trip stay visible in the API?
"""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, quote

import requests

API = "https://v6.db.transport.rest"
UA = "bahn-pb-debug/1.0"


def test_durations(station_id: str, products: dict, when_iso: str | None = None,
                   durations: list[int] | None = None) -> dict:
    """Fetch departures with different duration values and report counts.

    when_iso: optional ISO datetime; if None, uses 'now'.
    durations: list of minutes to try; defaults to a useful spread.
    """
    durations = durations or [60, 120, 240, 480, 720, 1440]
    results = {"when": when_iso or "now", "tests": []}

    for dur in durations:
        params = {
            "duration": dur,
            "results": 1000,
            "remarks": "false",
        }
        for p, e in products.items():
            params[p] = "true" if e else "false"
        if when_iso:
            params["when"] = when_iso

        url = f"{API}/stops/{station_id}/departures?{urlencode(params)}"
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            data = r.json()
            deps = data.get("departures") if isinstance(data, dict) else data
            deps = deps or []

            # Find actual time span of returned trips
            times = []
            for d in deps:
                t = d.get("plannedWhen") or d.get("when")
                if t:
                    times.append(t)
            times.sort()
            first = times[0] if times else None
            last = times[-1] if times else None

            # Compute span in minutes
            span_minutes = None
            if first and last:
                try:
                    dt_first = datetime.fromisoformat(first.replace("Z", "+00:00"))
                    dt_last = datetime.fromisoformat(last.replace("Z", "+00:00"))
                    span_minutes = int((dt_last - dt_first).total_seconds() // 60)
                except Exception:
                    pass

            # Count unique trip IDs
            unique_trip_ids = len({d.get("tripId") for d in deps
                                   if d.get("tripId")})

            results["tests"].append({
                "duration_requested": dur,
                "trips_returned": len(deps),
                "unique_trip_ids": unique_trip_ids,
                "earliest_when": first,
                "latest_when": last,
                "actual_span_minutes": span_minutes,
            })
        except Exception as e:
            results["tests"].append({
                "duration_requested": dur,
                "error": str(e),
            })

    return results


def test_trip_completeness(trip_id: str) -> dict:
    """Fetch a trip and report which stopovers have real-time data filled in."""
    url = f"{API}/trips/{quote(trip_id, safe='')}"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if r.status_code == 404:
            return {"trip_id": trip_id, "error": "404 Not Found"}
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        return {"trip_id": trip_id, "error": str(e)}

    trip = body.get("trip") or body
    stopovers = trip.get("stopovers") or []
    now = datetime.now(timezone.utc)

    summary = {
        "trip_id": trip_id,
        "line": (trip.get("line") or {}).get("name"),
        "origin": (trip.get("origin") or {}).get("name"),
        "destination": (trip.get("destination") or {}).get("name"),
        "cancelled": bool(trip.get("cancelled")),
        "stopovers_total": len(stopovers),
        "stopovers": [],
    }

    for sp in stopovers:
        stop = sp.get("stop") or {}
        planned_arr = sp.get("plannedArrival")
        actual_arr = sp.get("arrival")
        arrival_delay = sp.get("arrivalDelay")

        # Is this stop in the past (planned) relative to 'now'?
        in_past = None
        if planned_arr:
            try:
                dt = datetime.fromisoformat(planned_arr.replace("Z", "+00:00"))
                in_past = dt < now
            except Exception:
                pass

        summary["stopovers"].append({
            "stop": stop.get("name"),
            "planned_arrival": planned_arr,
            "actual_arrival": actual_arr,
            "arrival_delay_minutes": (int(arrival_delay // 60)
                                      if arrival_delay is not None else None),
            "cancelled": bool(sp.get("cancelled")),
            "in_past": in_past,
            "has_real_arrival_data": actual_arr is not None,
        })

    # Aggregate
    past_stops = [s for s in summary["stopovers"] if s["in_past"]]
    past_with_data = [s for s in past_stops if s["has_real_arrival_data"]]
    summary["past_stopovers"] = len(past_stops)
    summary["past_stopovers_with_arrival_data"] = len(past_with_data)

    return summary
