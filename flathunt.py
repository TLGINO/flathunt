# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Poll flat listings near the office and log matches to SQLite.

Usage:
    uv run flathunt.py            # fetch once
    uv run flathunt.py --serve    # map on http://localhost:8050, fetching every 15 min
"""

import argparse
import datetime as dt
import http.server
import json
import math
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OFFICE = "Elias-Canetti-Strasse 2, 8050 Zürich"
OFFICE_LAT, OFFICE_LON = 47.41232, 8.54164
MIN_ROOMS, MAX_ROOMS = 1.0, 4.5  # the map filters further; 2-2.5 is the default there
MAX_RENT = 2500  # CHF/month incl. utilities
MAX_DISTANCE_KM = 20  # straight-line prefilter before asking for a commute time
ARRIVE_BY = "08:30"
# Roughly Zürich city + Glattal + Winterthur direction
BBOX = dict(west=8.40, east=8.80, south=47.30, north=47.52)

UA = "flathunt/0.1 (personal flat search)"
DEFAULT_DB = Path(__file__).with_name("flats.db")


def get_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError):  # flaky network: retry
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


# --- sources -----------------------------------------------------------------
# Each source yields dicts with the keys of the `listings` table (minus bookkeeping columns).


def flatfox_pins(box):
    """The pin endpoint caps at 1000 results, so split the box into quadrants until it doesn't."""
    pins = get_json(
        "https://flatfox.ch/api/v1/pin/",
        dict(box, max_count=1000, object_category="APARTMENT", offer_type="RENT"),
    )
    if len(pins) < 1000:
        return {p["pk"] for p in pins}
    mid_lat, mid_lon = (box["south"] + box["north"]) / 2, (box["west"] + box["east"]) / 2
    pks = set()
    for south, north in ((box["south"], mid_lat), (mid_lat, box["north"])):
        for west, east in ((box["west"], mid_lon), (mid_lon, box["east"])):
            pks |= flatfox_pins(dict(west=west, east=east, south=south, north=north))
    return pks


def flatfox():
    pks = sorted(flatfox_pins(BBOX))
    for i in range(0, len(pks), 50):
        page = get_json("https://flatfox.ch/api/v1/public-listing/", {"pk": pks[i : i + 50], "limit": 50})
        for r in page["results"]:
            if r.get("offer_type") != "RENT" or r.get("object_category") != "APARTMENT":
                continue
            if r.get("rent_gross"):
                rent = r["rent_gross"]
            elif r.get("rent_net"):
                rent = r["rent_net"] + (r.get("rent_charges") or 0)
            else:
                rent = r.get("price_display")
            yield dict(
                source="flatfox",
                source_id=str(r["pk"]),
                url="https://flatfox.ch" + r["url"],
                title=r.get("description_title") or r.get("short_title"),
                address=r.get("public_address"),
                zipcode=r.get("zipcode"),
                city=r.get("city"),
                lat=r.get("latitude"),
                lon=r.get("longitude"),
                rooms=float(r["number_of_rooms"]) if r.get("number_of_rooms") else None,
                rent=rent,
                surface=r.get("surface_living"),
                moving_date=r.get("moving_date") or r.get("moving_date_type"),
                published=r.get("published"),
                is_temporary=int(bool(r.get("is_temporary"))),
                raw=json.dumps(r, ensure_ascii=False),
            )


# Homegate and ImmoScout24 sit behind Cloudflare / DataDome bot protection, so
# plain HTTP gets a 403. Add them here once SMG provides API access or allowlists us.
SOURCES = [flatfox]


# --- commute -----------------------------------------------------------------
# Walking and bike times come from OSRM (routing.openstreetmap.de), up to 100 flats per request.
# Public transport is looked up per stop, not per flat: a flat's time is the walk to its nearest
# stop, or to its nearest tram/train stop if that is faster, plus that stop's cached ride to the
# office. Nearby flats share stops, so once the cache is warm new flats need no lookups at all.

OSRM = "https://routing.openstreetmap.de/routed-{profile}/table/v1/driving/"
STOPS_URL = ("https://data.sbb.ch/api/explore/v2.1/catalog/datasets/"
             "dienststellen-gemass-opentransportdataswiss/exports/json")
CONNECTIONS_URL = "https://transport.opendata.ch/v1/connections"
STOPS_TTL_DAYS = 30        # re-download the stop list monthly
RIDES_TTL_DAYS = 60        # timetables change every December; refresh cached rides every two months
TRANSIT_BUDGET_S = 8 * 60  # time per run for ride lookups
RATE_LIMIT_PAUSE_S = 75    # the API allows ~30 quick requests, then blocks for about a minute
RAIL_STOP_MAX_KM = 1.5     # how far we'd walk to reach a tram or train stop instead of the nearest bus
# Getting to the platform. Calibrated against 87 per-flat lookups: with 2 min the stop-based
# estimate averages +0.1 min off, and 94% are within 3 min.
STOP_ACCESS_MIN = 2


def next_weekday():
    d = dt.date.today() + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def walk_minutes(km):
    return round(km * 1.3 / 5 * 60)  # detour factor 1.3, 5 km/h; used for the walk to a stop


def ride_minutes(stop_number):
    """Minutes from a stop to the office door on public transport, arriving by ARRIVE_BY."""
    data = get_json(CONNECTIONS_URL, {"from": stop_number, "to": OFFICE, "date": next_weekday().isoformat(),
                                      "time": ARRIVE_BY, "isArrivalTime": 1, "limit": 4})
    durations = []
    for c in data.get("connections", []):
        days, hms = c["duration"].split("d")  # "00d00:18:00"
        h, m, _ = hms.split(":")
        durations.append(int(days) * 1440 + int(h) * 60 + int(m))
    return min(durations) if durations else None


def osrm_minutes(profile, points):
    """Route minutes from each (lat, lon) in points to the office."""
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in [(OFFICE_LAT, OFFICE_LON), *points])
    data = get_json(OSRM.format(profile=profile) + coords,
                    {"sources": ";".join(str(i) for i in range(1, len(points) + 1)), "destinations": 0,
                     "annotations": "duration"})
    return [None if row[0] is None else round(row[0] / 60) for row in data["durations"]]


def update_routes(con):
    """Fill in routed walking and bike times for flats that don't have them yet."""
    rows = con.execute(
        "SELECT source, source_id, lat, lon FROM listings WHERE gone_at IS NULL AND distance_km <= ? "
        "AND (walk_routed IS NULL OR bike_min IS NULL)", (MAX_DISTANCE_KM,)
    ).fetchall()
    done = 0
    for i in range(0, len(rows), 100):
        batch = rows[i : i + 100]
        points = [(lat, lon) for _, _, lat, lon in batch]
        try:
            walks, bikes = osrm_minutes("foot", points), osrm_minutes("bike", points)
        except Exception as e:  # routing is best-effort; the walk keeps its straight-line estimate
            print(f"routing failed: {e}", file=sys.stderr)
            break
        for (src, sid, _, _), walk, bike in zip(batch, walks, bikes):
            con.execute(
                "UPDATE listings SET walk_min=coalesce(?, walk_min), walk_routed=1, bike_min=? "
                "WHERE source=? AND source_id=?", (walk, bike, src, sid))
        done += len(batch)
        time.sleep(1)
    con.commit()
    if rows:
        print(f"routes: {done} of {len(rows)} flats routed for walking and bike")


def load_stops(con):
    loaded = con.execute("SELECT value FROM meta WHERE key='stops_loaded'").fetchone()
    if loaded and dt.datetime.fromisoformat(loaded[0]) > dt.datetime.now() - dt.timedelta(days=STOPS_TTL_DAYS):
        return
    pad = 0.03  # include stops just outside the search box
    where = (f"stoppoint='true' and in_bbox(geopos, {BBOX['south'] - pad}, {BBOX['west'] - pad}, "
             f"{BBOX['north'] + pad}, {BBOX['east'] + pad})")
    try:
        stops = get_json(STOPS_URL, {"where": where, "select": "number,designationofficial,geopos,meansoftransport"})
    except Exception as e:
        print(f"stop list download failed: {e}", file=sys.stderr)
        return
    con.execute("DELETE FROM stops")
    con.executemany(
        "INSERT INTO stops VALUES (?, ?, ?, ?, ?)",
        [(s["number"], s["designationofficial"], s["geopos"]["lat"], s["geopos"]["lon"],
          int(any(m in (s["meansoftransport"] or "") for m in ("TRAIN", "TRAM")))) for s in stops if s.get("geopos")],
    )
    con.execute("INSERT OR REPLACE INTO meta VALUES ('stops_loaded', ?)", (dt.datetime.now().isoformat(),))
    con.commit()
    print(f"stops: loaded {len(stops)} stops")


def candidate_stops(lat, lon, stops):
    """The nearest stop, plus the nearest tram/train stop within RAIL_STOP_MAX_KM: [(number, walk_min)]."""
    kx = math.cos(math.radians(lat)) * 111.32
    nearest = nearest_rail = None
    for number, slat, slon, rail in stops:
        d = math.hypot((slon - lon) * kx, (slat - lat) * 110.57)
        if nearest is None or d < nearest[1]:
            nearest = (number, d)
        if rail and d <= RAIL_STOP_MAX_KM and (nearest_rail is None or d < nearest_rail[1]):
            nearest_rail = (number, d)
    found = {c[0]: walk_minutes(c[1]) for c in (nearest, nearest_rail) if c}
    return list(found.items())


def update_transit(con):
    load_stops(con)
    stops = con.execute("SELECT number, lat, lon, rail FROM stops").fetchall()
    if not stops:
        return
    names = dict(con.execute("SELECT number, name FROM stops"))
    rides = {n: (m, f) for n, m, f in con.execute("SELECT number, minutes, fetched FROM stop_rides")}
    rows = con.execute(
        "SELECT source, source_id, lat, lon, walk_min FROM listings WHERE gone_at IS NULL AND distance_km <= ? "
        "AND transit_src IS NOT 'direct'", (MAX_DISTANCE_KM,)
    ).fetchall()
    cands = {(src, sid): candidate_stops(lat, lon, stops) for src, sid, lat, lon, _ in rows}

    # look up missing rides first, the stops shared by most flats first; then refresh stale ones
    use = {}
    for cs in cands.values():
        for n, _ in cs:
            use[n] = use.get(n, 0) + 1
    stale_before = (dt.datetime.now() - dt.timedelta(days=RIDES_TTL_DAYS)).isoformat()
    missing = sorted((n for n in use if n not in rides), key=lambda n: -use[n])
    stale = sorted((n for n in use if n in rides and rides[n][1] < stale_before), key=lambda n: rides[n][1])
    queue, done, deadline = missing + stale, 0, time.monotonic() + TRANSIT_BUDGET_S
    while queue and time.monotonic() < deadline:
        n = queue[0]
        try:
            minutes = ride_minutes(n)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(RATE_LIMIT_PAUSE_S)
                continue
            print(f"ride lookup failed for stop {n}: {e}", file=sys.stderr)
            queue.pop(0)
            continue
        except Exception as e:
            print(f"ride lookup failed for stop {n}: {e}", file=sys.stderr)
            queue.pop(0)
            continue
        now = dt.datetime.now().isoformat(timespec="seconds")
        con.execute("INSERT OR REPLACE INTO stop_rides VALUES (?, ?, ?)", (n, minutes, now))
        rides[n] = (minutes, now)
        queue.pop(0)
        done += 1
        if done % 25 == 0:
            con.commit()
        time.sleep(1)
    con.commit()
    still_missing = sum(1 for n in use if n not in rides)
    print(f"transit: {done} stop rides looked up, {len(rides)} cached, {still_missing} still missing")

    # a flat's transit time is only final once all of its candidate stops are known
    for (src, sid), cs in cands.items():
        if not cs or any(n not in rides for n, _ in cs):
            continue
        options = [(walk + STOP_ACCESS_MIN + rides[n][0], n) for n, walk in cs if rides[n][0] is not None]
        transit, via = min(options) if options else (None, None)
        con.execute(
            "UPDATE listings SET transit_min=?, transit_src='stop', transit_via=?, "
            "commute_min=min(walk_min, coalesce(?, walk_min)) WHERE source=? AND source_id=?",
            (transit, names.get(via), transit, src, sid))
    con.commit()


# --- storage -----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT, title TEXT, address TEXT, zipcode TEXT, city TEXT,
    lat REAL, lon REAL, rooms REAL, rent INTEGER, surface REAL,
    moving_date TEXT, published TEXT, is_temporary INTEGER,
    distance_km REAL, walk_min INTEGER, transit_min INTEGER, commute_min INTEGER,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, gone_at TEXT,
    raw TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS runs (
    started TEXT, finished TEXT, source TEXT, seen INTEGER, matched INTEGER, new INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS stops (number INTEGER PRIMARY KEY, name TEXT, lat REAL, lon REAL, rail INTEGER);
CREATE TABLE IF NOT EXISTS stop_rides (number INTEGER PRIMARY KEY, minutes INTEGER, fetched TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIEW IF NOT EXISTS matches AS
    SELECT first_seen, commute_min, walk_min, transit_min, rent, rooms, surface, address, url, source
    FROM listings
    WHERE gone_at IS NULL AND commute_min <= 30
    ORDER BY first_seen DESC, commute_min;
"""

# columns added after the first version; existing databases get them on the next run
NEW_COLUMNS = {
    "bike_min": "INTEGER",
    "walk_routed": "INTEGER",   # 1 once walk_min comes from OSRM instead of the straight-line estimate
    "transit_src": "TEXT",      # 'direct' (older per-flat lookups) or 'stop' (via the stop cache)
    "transit_via": "TEXT",      # stop the transit estimate starts from
}


def migrate(con):
    have = {r[1] for r in con.execute("PRAGMA table_info(listings)")}
    for col, typ in NEW_COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE listings ADD COLUMN {col} {typ}")
            if col == "transit_src":
                con.execute("UPDATE listings SET transit_src='direct' WHERE transit_min IS NOT NULL")
    con.commit()


def matches_filters(x):
    return (
        x["rooms"] is not None and MIN_ROOMS <= x["rooms"] <= MAX_ROOMS
        and x["rent"] is not None and x["rent"] <= MAX_RENT
        and x["lat"] is not None and x["lon"] is not None
    )


def run(db):
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    migrate(con)
    now = dt.datetime.now().isoformat(timespec="seconds")

    for source in SOURCES:
        seen = matched = new = 0
        err = None
        try:
            for x in source():
                seen += 1
                if not matches_filters(x):
                    continue
                matched += 1
                key = (x["source"], x["source_id"])
                cols = [k for k in x if k not in ("source", "source_id")]
                exists = con.execute("SELECT 1 FROM listings WHERE source=? AND source_id=?", key).fetchone()
                if exists:
                    con.execute(
                        f"UPDATE listings SET {', '.join(f'{c}=?' for c in cols)}, last_seen=?, gone_at=NULL "
                        "WHERE source=? AND source_id=?",
                        [x[c] for c in cols] + [now, *key],
                    )
                    continue
                km = haversine_km(OFFICE_LAT, OFFICE_LON, x["lat"], x["lon"])
                walk = walk_minutes(km)  # replaced by a routed time in update_routes
                con.execute(
                    f"INSERT INTO listings (source, source_id, {', '.join(cols)}, distance_km, walk_min, "
                    "commute_min, first_seen, last_seen) "
                    f"VALUES (?, ?, {', '.join('?' * len(cols))}, ?, ?, ?, ?, ?)",
                    [*key, *[x[c] for c in cols], round(km, 2), walk, walk, now, now],
                )
                new += 1
            # anything from this source not seen in this run has been taken down
            con.execute(
                "UPDATE listings SET gone_at=? WHERE source=? AND last_seen<? AND gone_at IS NULL",
                (now, source.__name__, now),
            )
        except Exception as e:
            err = repr(e)
            print(f"{source.__name__} failed: {e}", file=sys.stderr)
        con.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, dt.datetime.now().isoformat(timespec="seconds"), source.__name__, seen, matched, new, err),
        )
        con.commit()
        print(f"{source.__name__}: {seen} seen, {matched} match rooms/rent, {new} new")

    update_routes(con)
    update_transit(con)
    for commute, rent, rooms, address, url in con.execute(
        "SELECT commute_min, rent, rooms, address, url FROM listings WHERE first_seen=? AND commute_min <= 30 "
        "ORDER BY commute_min", (now,)
    ):
        print(f"NEW  {commute:>3} min  CHF {rent:>5}  {rooms} Zi  {address}  {url}")
    con.close()


# --- site --------------------------------------------------------------------
# site/index.html is a static page that reads site/listings.json, so the same
# folder works on localhost (--serve) and on GitHub Pages.

SITE = Path(__file__).with_name("site")


def write_listings_json(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT source, url, address, lat, lon, rooms, rent, surface, is_temporary, first_seen, walk_min, "
        "walk_routed, bike_min, transit_min, transit_src, transit_via FROM listings "
        "WHERE gone_at IS NULL AND distance_km <= ?", (MAX_DISTANCE_KM,)
    ).fetchall()
    last = con.execute("SELECT max(finished) FROM runs WHERE error IS NULL").fetchone()[0]
    con.close()
    listings = [
        dict(source=r["source"], url=r["url"], address=r["address"], lat=r["lat"], lon=r["lon"],
             rooms=r["rooms"], rent=r["rent"], surface=r["surface"], temporary=bool(r["is_temporary"]),
             first_seen=r["first_seen"],
             # None means "not known yet"; the page shows those as pending
             walk=r["walk_min"] if r["walk_routed"] else None,
             bike=r["bike_min"],
             transit=r["transit_min"] if r["transit_src"] else None,
             via=r["transit_via"])
        for r in rows
    ]
    data = dict(office=dict(name=OFFICE, lat=OFFICE_LAT, lon=OFFICE_LON), listings=listings,
                updated=(last or "never").replace("T", " ")[:16])
    tmp = SITE / "listings.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(SITE / "listings.json")  # atomic, so the server never serves half a file
    print(f"site: {len(listings)} listings -> {SITE / 'listings.json'}")


def update(db):
    run(db)
    write_listings_json(db)


def serve(db, port, poll_minutes):
    write_listings_json(db)  # serve current data right away, before the first poll finishes
    if poll_minutes:
        def poll():
            while True:
                try:
                    update(db)
                except Exception as e:
                    print(f"poll failed: {e}", file=sys.stderr)
                time.sleep(poll_minutes * 60)
        threading.Thread(target=poll, daemon=True).start()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(SITE), **kw)

        def log_message(self, *args):
            pass

    print(f"serving on http://localhost:{port}" + (f", polling every {poll_minutes} min" if poll_minutes else ""))
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--serve", action="store_true", help="serve the map on localhost")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--poll", type=int, default=15, metavar="MIN",
                    help="with --serve: fetch new listings every MIN minutes (0 = never)")
    args = ap.parse_args()
    if args.serve:
        serve(args.db, args.port, args.poll)
    else:
        update(args.db)
