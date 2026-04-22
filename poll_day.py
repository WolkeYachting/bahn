"""Polls the Bahn API and maintains a per-day state-machine of trips.

Status lifecycle for each trip:
  scheduled  - known to belong to today's plan, but never seen with live data
               (only possible if discovered via departures lookahead before the
               train starts, OR if the train somehow runs before the first poll
               of the day — see force-finish behavior in close.py)
  active     - last poll saw this trip in the departures list
  finished   - last poll did NOT see this trip in departures, AND the planned
               arrival at the train's destination is at least
               `finish_grace_minutes` in the past

Each poll:
1. Determine the current logical day (Europe/Berlin, with 03:30 boundary)
2. Load that day's log (or create it)
3. Fetch departures for the next 24h from the configured station, filtered by
   product
4. For each departure: assign it to a logical day (might be today or tomorrow,
   especially during overnight hours near the boundary). Fetch full trip data.
5. Update the appropriate day-log:
   - new trip  -> add as 'active' with full trip snapshot
   - existing  -> update snapshot, keep status 'active'
6. Mark trips that were NOT in this poll's departures as candidates for
   'finished' (transition only if planned arrival + grace is past).
"""

import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode, quote

import requests

from storage import Storage
from timeutil import (
    BERLIN, now_berlin, now_iso_utc, parse_iso,
    logical_day_for, current_logical_day,
)

API = "https://v6.db.transport.rest"
BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
UA = "bahn-pb/1.0"

_storage = Storage()


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_day_log(date: str) -> dict:
    filename = f"logs/{date}.json"
    _storage.hydrate(filename)
    raw = _storage.read(filename)
    if not raw:
        return {
            "date": date,
            "closed": False,
            "trips": {},
            "polls": [],
        }
    return json.loads(raw)


def save_day_log(log: dict, commit_message: str) -> None:
    filename = f"logs/{log['date']}.json"
    _storage.write(
        filename,
        json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True),
        commit_message=commit_message,
    )


def _get_with_retry(url: str, max_retries: int = 3, backoff_seconds: float = 2.0):
    """GET with retry on 5xx errors. The Bahn API is occasionally flaky."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if r.status_code >= 500 and attempt < max_retries - 1:
                print(f"[api] {r.status_code} on attempt {attempt+1}, retrying...", file=sys.stderr)
                time.sleep(backoff_seconds * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            last_exc = e
            status = e.response.status_code if e.response is not None else None
            if status and status >= 500 and attempt < max_retries - 1:
                print(f"[api] HTTPError {status} on attempt {attempt+1}, retrying...", file=sys.stderr)
                time.sleep(backoff_seconds * (attempt + 1))
                continue
            raise
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                print(f"[api] {type(e).__name__} on attempt {attempt+1}, retrying...", file=sys.stderr)
                time.sleep(backoff_seconds * (attempt + 1))
                continue
            raise
    if last_exc:
        raise last_exc


def fetch_departures(station_id: str, products: dict, duration_minutes: int) -> list:
    params = {
        "duration": duration_minutes,
        "results": 1000,
        "remarks": "false",
        "profile": "db",  # The 'db' profile consistently returns more trips
    }
    for product, enabled in products.items():
        params[product] = "true" if enabled else "false"
    url = f"{API}/stops/{station_id}/departures?{urlencode(params)}"
    r = _get_with_retry(url)
    data = r.json()
    if isinstance(data, dict) and "departures" in data:
        return data["departures"]
    return data or []


def fetch_trip(trip_id: str) -> dict | None:
    url = f"{API}/trips/{quote(trip_id, safe='')}?profile=db"
    try:
        r = _get_with_retry(url)
        body = r.json()
        return body.get("trip") or body
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        print(f"[trip] fetch failed for {trip_id[:50]}...: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[trip] fetch failed for {trip_id[:50]}...: {e}", file=sys.stderr)
        return None


def normalize_stopover(sp: dict) -> dict:
    stop = sp.get("stop") or {}
    def _mins(secs):
        return None if secs is None else int(secs // 60)
    return {
        "stop_id": stop.get("id"),
        "stop_name": stop.get("name"),
        "planned_arrival": sp.get("plannedArrival"),
        "arrival": sp.get("arrival"),
        "arrival_delay_minutes": _mins(sp.get("arrivalDelay")),
        "planned_departure": sp.get("plannedDeparture"),
        "departure": sp.get("departure"),
        "departure_delay_minutes": _mins(sp.get("departureDelay")),
        "cancelled": bool(sp.get("cancelled")),
        "arrival_platform": sp.get("arrivalPlatform"),
        "planned_arrival_platform": sp.get("plannedArrivalPlatform"),
    }


def build_trip_entry(trip: dict, station_id: str) -> dict | None:
    stopovers = trip.get("stopovers") or []
    norm = [normalize_stopover(sp) for sp in stopovers]
    idx = next((i for i, sp in enumerate(norm)
                if sp["stop_id"] == station_id), None)
    if idx is None:
        return None
    line = trip.get("line") or {}
    return {
        "trip_id": trip.get("id") or trip.get("tripId"),
        "line_name": line.get("name"),
        "product": line.get("product"),
        "origin": (trip.get("origin") or {}).get("name"),
        "destination": (trip.get("destination") or {}).get("name"),
        "direction": trip.get("direction"),
        "cancelled": bool(trip.get("cancelled")),
        "station_stopover_index": idx,
        "stopovers": norm,
    }


def planned_departure_at_station(trip_entry: dict) -> str | None:
    idx = trip_entry.get("station_stopover_index")
    sps = trip_entry.get("stopovers") or []
    if idx is None or idx >= len(sps):
        return None
    sp = sps[idx]
    return sp.get("planned_departure") or sp.get("planned_arrival")


def planned_arrival_at_destination(trip_entry: dict) -> str | None:
    sps = trip_entry.get("stopovers") or []
    if not sps:
        return None
    last = sps[-1]
    return last.get("planned_arrival") or last.get("planned_departure")


def assign_logical_day(trip_entry: dict, config: dict) -> str | None:
    """Logical day is determined by planned departure at the configured station."""
    pd = planned_departure_at_station(trip_entry)
    dt = parse_iso(pd)
    if dt is None:
        return None
    return logical_day_for(dt, config["day_boundary_hour"],
                           config["day_boundary_minute"])


def transition_to_finished(trip: dict, now_utc, grace_minutes: int) -> bool:
    """Whether a trip currently 'active' should now become 'finished'.

    Condition: planned arrival at destination is at least grace_minutes in the
    past AND the trip was not seen in this poll's departures.
    """
    arr = parse_iso(planned_arrival_at_destination(trip))
    if arr is None:
        # No info about destination arrival — be conservative and finish
        return True
    return now_utc >= arr + timedelta(minutes=grace_minutes)


def poll_once() -> dict:
    config = load_config()
    station = config["station"]
    station_id = station["id"]
    bh, bm = config["day_boundary_hour"], config["day_boundary_minute"]
    grace = config.get("force_finish_grace_minutes", 30)

    started_iso = now_iso_utc()
    now_utc = now_berlin().astimezone(BERLIN).utcoffset()  # placeholder
    # Use a UTC-aware "now" for arithmetic
    from datetime import datetime, timezone as _tz
    now_utc = datetime.now(_tz.utc)

    stats = {
        "started_at": started_iso,
        "departures_seen": 0,
        "trips_fetched": 0,
        "trips_failed": 0,
        "trips_skipped_no_station": 0,
        "trips_added_today": 0,
        "trips_added_other_day": 0,
        "trips_marked_finished": 0,
        "days_touched": [],
    }

    # 1. Fetch departures. The API silently caps 'duration' at ~60 min
    #    regardless of what we pass, so we just ask for 60.
    try:
        departures = fetch_departures(
            station_id, config["products"], duration_minutes=60
        )
    except Exception as e:
        stats["error"] = f"departures fetch failed: {e}"
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return stats

    stats["departures_seen"] = len(departures)

    # Group seen trip_ids by logical day for later finish-detection
    seen_trip_ids: set[str] = set()
    seen_by_day: dict[str, set[str]] = {}

    # We collect updates by day so we only load/save each day-log once
    day_updates: dict[str, dict] = {}  # date -> log dict

    def get_day(date: str) -> dict:
        if date not in day_updates:
            day_updates[date] = load_day_log(date)
            day_updates[date].setdefault("station_id", station_id)
            day_updates[date].setdefault("station_name", station["name"])
        return day_updates[date]

    # 2. For each departure, fetch full trip and place it in the right day-log
    request_delay = config.get("request_delay_seconds", 1.0)
    for dep in departures:
        trip_id = dep.get("tripId")
        if not trip_id or trip_id in seen_trip_ids:
            continue
        seen_trip_ids.add(trip_id)

        time.sleep(request_delay)
        trip = fetch_trip(trip_id)
        if trip is None:
            stats["trips_failed"] += 1
            continue

        entry = build_trip_entry(trip, station_id)
        if entry is None:
            stats["trips_skipped_no_station"] += 1
            continue

        day = assign_logical_day(entry, config)
        if day is None:
            stats["trips_skipped_no_station"] += 1
            continue

        seen_by_day.setdefault(day, set()).add(trip_id)

        log = get_day(day)
        existing = log["trips"].get(trip_id)
        is_today = day == current_logical_day(bh, bm)

        if existing is None:
            log["trips"][trip_id] = {
                "data": entry,
                "status": "active",
                "first_seen_at": started_iso,
                "last_seen_at": started_iso,
                "force_finished": False,
            }
            stats["trips_fetched"] += 1
            if is_today:
                stats["trips_added_today"] += 1
            else:
                stats["trips_added_other_day"] += 1
        else:
            existing["data"] = entry
            existing["last_seen_at"] = started_iso
            # If a trip was previously finished (e.g. API briefly hid it and
            # then it reappeared), reactivate. This is unusual but harmless.
            existing["status"] = "active"
            stats["trips_fetched"] += 1

    # 3. For all known days touched in this poll, transition still-active
    #    but not-seen-this-poll trips to 'finished' if they're past their
    #    planned arrival.
    #    We also need to consider days that we already have logs for but
    #    didn't touch in this poll (yesterday's tail, for example).
    today = current_logical_day(bh, bm)
    yesterday = (parse_iso(today + "T00:00:00+00:00").date()
                 - timedelta(days=1)).isoformat()
    candidate_days = set(day_updates.keys()) | {today, yesterday}

    for day in candidate_days:
        if day not in day_updates:
            log = load_day_log(day)
            if log.get("closed"):
                continue
            # Only persist if we actually change something
            day_updates[day] = log
        log = day_updates[day]
        if log.get("closed"):
            continue
        seen_today = seen_by_day.get(day, set())
        for trip_id, t in log["trips"].items():
            if t.get("status") != "active":
                continue
            if trip_id in seen_today:
                continue
            if transition_to_finished(t.get("data", {}), now_utc, grace):
                t["status"] = "finished"
                t["finished_at"] = started_iso
                stats["trips_marked_finished"] += 1

    # 4. Persist all touched day-logs
    finished_at = now_iso_utc()
    stats["finished_at"] = finished_at
    stats["days_touched"] = sorted(day_updates.keys())

    for day, log in day_updates.items():
        log.setdefault("polls", []).append({
            "started_at": started_iso,
            "finished_at": finished_at,
            "trips_seen_in_poll": len(seen_by_day.get(day, set())),
        })
        # Cap poll history per day
        log["polls"] = log["polls"][-200:]
        commit_msg = (
            f"poll {day}: +{sum(1 for t in log['trips'].values() if t['status'] == 'active')} active, "
            f"{sum(1 for t in log['trips'].values() if t['status'] == 'finished')} finished"
        )
        save_day_log(log, commit_message=commit_msg)

    print(json.dumps(stats, indent=2, ensure_ascii=False))
    return stats


if __name__ == "__main__":
    poll_once()
