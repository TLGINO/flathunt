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


RATE_LIMITED = False


def next_weekday():
    d = dt.date.today() + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


class RateLimited(Exception):
    pass


def transit_minutes(lat, lon):
    """Door-to-door public transport minutes (incl. walking legs), arriving by ARRIVE_BY."""
    global RATE_LIMITED
    if RATE_LIMITED:
        raise RateLimited
    try:
        data = get_json(
            "https://transport.opendata.ch/v1/connections",
            {"from": f"{lat},{lon}", "to": OFFICE, "date": next_weekday().isoformat(),
             "time": ARRIVE_BY, "isArrivalTime": 1, "limit": 4},
        )
    except urllib.error.HTTPError as e:
        if e.code == 429:  # back off for the rest of this run; missing times are retried next run
            RATE_LIMITED = True
            print("transport API rate limit hit, deferring remaining commute lookups", file=sys.stderr)
            raise RateLimited from e
        raise
    durations = []
    for c in data.get("connections", []):
        d = c["duration"]  # "00d00:18:00"
        days, hms = d.split("d")
        h, m, _ = hms.split(":")
        durations.append(int(days) * 1440 + int(h) * 60 + int(m))
    return min(durations) if durations else None


def walk_minutes(km):
    return round(km * 1.3 / 5 * 60)  # detour factor 1.3, 5 km/h


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
CREATE VIEW IF NOT EXISTS matches AS
    SELECT first_seen, commute_min, walk_min, transit_min, rent, rooms, surface, address, url, source
    FROM listings
    WHERE gone_at IS NULL AND commute_min <= 30
    ORDER BY first_seen DESC, commute_min;
"""


def matches_filters(x):
    return (
        x["rooms"] is not None and MIN_ROOMS <= x["rooms"] <= MAX_ROOMS
        and x["rent"] is not None and x["rent"] <= MAX_RENT
        and x["lat"] is not None and x["lon"] is not None
    )


def run(db):
    global RATE_LIMITED
    RATE_LIMITED = False
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
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
                row = con.execute(
                    "SELECT commute_min, transit_min FROM listings WHERE source=? AND source_id=?", key
                ).fetchone()
                cols = [k for k in x if k not in ("source", "source_id")]
                if row:
                    con.execute(
                        f"UPDATE listings SET {', '.join(f'{c}=?' for c in cols)}, last_seen=?, gone_at=NULL "
                        "WHERE source=? AND source_id=?",
                        [x[c] for c in cols] + [now, *key],
                    )
                    continue
                km = haversine_km(OFFICE_LAT, OFFICE_LON, x["lat"], x["lon"])
                walk = walk_minutes(km)
                transit = None
                if km <= MAX_DISTANCE_KM:
                    try:
                        transit = transit_minutes(x["lat"], x["lon"])
                    except RateLimited:
                        pass
                    except Exception as e:  # commute is best-effort; retried on a later run
                        print(f"commute lookup failed for {x['url']}: {e}", file=sys.stderr)
                    else:
                        time.sleep(1)
                commute = min(v for v in (walk, transit) if v is not None)
                con.execute(
                    f"INSERT INTO listings (source, source_id, {', '.join(cols)}, distance_km, walk_min, "
                    "transit_min, commute_min, first_seen, last_seen) "
                    f"VALUES (?, ?, {', '.join('?' * len(cols))}, ?, ?, ?, ?, ?, ?)",
                    [*key, *[x[c] for c in cols], round(km, 2), walk, transit, commute, now, now],
                )
                new += 1
                if commute <= 30:
                    print(f"NEW  {commute:>3} min  CHF {x['rent']:>5}  {x['rooms']} Zi  {x['address']}  {x['url']}")
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

    # retry commute lookups that failed on earlier runs
    for sid, src, lat, lon, walk in con.execute(
        "SELECT source_id, source, lat, lon, walk_min FROM listings "
        "WHERE transit_min IS NULL AND distance_km <= ? AND gone_at IS NULL", (MAX_DISTANCE_KM,)
    ).fetchall():
        try:
            t = transit_minutes(lat, lon)
        except RateLimited:
            break
        except Exception:
            continue
        if t is not None:
            con.execute(
                "UPDATE listings SET transit_min=?, commute_min=? WHERE source=? AND source_id=?",
                (t, min(t, walk), src, sid),
            )
        time.sleep(1)
    con.commit()
    con.close()


# --- site --------------------------------------------------------------------
# site/index.html is a static page that reads site/listings.json, so the same
# folder works on localhost (--serve) and on GitHub Pages.

SITE = Path(__file__).with_name("site")


def write_listings_json(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT source, url, address, lat, lon, rooms, rent, surface, is_temporary, first_seen, "
        "walk_min, transit_min, commute_min FROM listings WHERE gone_at IS NULL "
        "AND (commute_min <= 30 OR (transit_min IS NULL AND distance_km <= ?))", (MAX_DISTANCE_KM,)
    ).fetchall()
    last = con.execute("SELECT max(finished) FROM runs WHERE error IS NULL").fetchone()[0]
    con.close()
    listings = [
        dict(source=r["source"], url=r["url"], address=r["address"], lat=r["lat"], lon=r["lon"],
             rooms=r["rooms"], rent=r["rent"], surface=r["surface"], temporary=bool(r["is_temporary"]),
             first_seen=r["first_seen"], walk=r["walk_min"], transit=r["transit_min"],
             # walk-only estimates over 30 min are not final until transit is known
             commute=r["commute_min"] if r["commute_min"] <= 30 else None)
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
