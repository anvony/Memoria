"""Home detection and trip clustering — pure queries over indexed photos.

Home is auto-detected: cluster all GPS points to a ~10km grid and take the
cell holding the most *days* of photography (days, not photos — a single
800-shot wedding shouldn't outvote three years of daily life). Overridable
via the kv table ("home_override" = "lat,lng").

A trip is a run of consecutive days whose photos are ≥ MIN_KM from home,
with 1-day gaps tolerated (a quiet travel day shouldn't split one trip in
two). Photos WITHOUT GPS taken on those same days are pulled in by
timestamp — that's how DSLR shots join the trip their phone photos prove
happened.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from . import db

MIN_KM = 100.0        # this far from home counts as "away"
MIN_PHOTOS = 8        # fewer than this is an errand, not a trip
GAP_DAYS = 1          # allow one photo-less day inside a trip


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def detect_home() -> tuple[float, float] | None:
    override = db.kv_get("home_override")
    if override:
        lat, lng = override.split(",")
        return float(lat), float(lng)

    rows = db.get_conn().execute(
        "SELECT lat, lng, substr(date_taken, 1, 10) AS day FROM photos "
        "WHERE lat IS NOT NULL AND live_of IS NULL AND hidden = 0 AND private = 0"
    ).fetchall()
    if not rows:
        return None

    # ~10km grid cells; count distinct DAYS per cell
    cell_days: dict[tuple[int, int], set[str]] = defaultdict(set)
    for r in rows:
        cell = (round(r["lat"] * 10), round(r["lng"] * 10))
        cell_days[cell].add(r["day"])
    best_cell = max(cell_days, key=lambda c: len(cell_days[c]))
    # centroid of the winning cell's points
    pts = [(r["lat"], r["lng"]) for r in rows
           if (round(r["lat"] * 10), round(r["lng"] * 10)) == best_cell]
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def detect_trips() -> list[dict]:
    """Returns [{id, place, start_date, end_date, photo_hashes}] newest first."""
    home = detect_home()
    if home is None:
        return []

    conn = db.get_conn()
    rows = conn.execute(
        "SELECT hash, lat, lng, place, substr(date_taken, 1, 10) AS day FROM photos "
        "WHERE live_of IS NULL AND hidden = 0 AND private = 0 ORDER BY date_taken"
    ).fetchall()

    # Photos the user removed from a specific trip (trips are auto-derived, so a
    # removal only sticks if we remember it and subtract it back out here). Keyed
    # by trip_id, applied when each run is closed below.
    excludes: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT trip_id, photo_hash FROM trip_excludes"):
        excludes[r["trip_id"]].add(r["photo_hash"])

    # Whole trips the user deleted — auto-detection would otherwise re-surface
    # them on every scan, so skip any run whose id was hidden.
    hidden_trips = {r["trip_id"] for r in conn.execute("SELECT trip_id FROM trip_hidden")}

    # Which days were "away days"?
    day_away: dict[str, list] = defaultdict(list)   # day -> away gps rows
    day_all: dict[str, list] = defaultdict(list)    # day -> all rows
    for r in rows:
        day_all[r["day"]].append(r)
        if r["lat"] is not None and _haversine_km(home[0], home[1], r["lat"], r["lng"]) >= MIN_KM:
            day_away[r["day"]].append(r)

    away_days = sorted(day_away)
    trips: list[dict] = []
    run: list[str] = []

    def close_run() -> None:
        if not run:
            return
        # all photos on the run's days (GPS-less ones join via timestamp)
        hashes = [r["hash"] for d in run for r in day_all[d]]
        places = Counter(
            r["place"] for d in run for r in day_away[d] if r["place"]
        )
        place = places.most_common(1)[0][0] if places else "Unknown"
        trip_id = f"trip-{run[0]}-{_slug(place)}"
        if trip_id in hidden_trips:   # user deleted this whole trip
            run.clear()
            return
        # Subtract any photos the user explicitly removed from this trip.
        removed = excludes.get(trip_id)
        if removed:
            hashes = [h for h in hashes if h not in removed]
        if len(hashes) < MIN_PHOTOS:
            run.clear()
            return
        trips.append({
            "id": trip_id,
            "place": place,
            "start_date": run[0],
            "end_date": run[-1],
            "photo_hashes": hashes,
        })
        run.clear()

    prev = None
    for day in away_days:
        if prev is not None and _day_gap(prev, day) > GAP_DAYS + 1:
            close_run()
        run.append(day)
        prev = day
    close_run()

    trips.sort(key=lambda t: t["start_date"], reverse=True)
    return trips


def _day_gap(a: str, b: str) -> int:
    from datetime import date
    da = date.fromisoformat(a)
    dbb = date.fromisoformat(b)
    return (dbb - da).days


def _slug(s: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in s.lower()).strip("-")
