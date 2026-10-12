"""SwayWay local server: serves the app and looks up official vote centers.

Polling places come from the Google Civic Information API (nationwide, needs a free
key in the GOOGLE_CIVIC_KEY environment variable) and, for California, from the
Secretary of State's vote center lookup (caearlyvoting.sos.ca.gov). Addresses are
matched with the U.S. Census Bureau geocoder.
"""
import http.cookiejar, json, math, os, re, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8642))
HOST = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
SOS = "https://caearlyvoting.sos.ca.gov"
CIVIC_KEY = os.environ.get("GOOGLE_CIVIC_KEY", "").strip()
CA_COUNTIES = ["Alameda", "Alpine", "Amador", "Butte", "Calaveras", "Colusa", "Contra Costa", "Del Norte",
    "El Dorado", "Fresno", "Glenn", "Humboldt", "Imperial", "Inyo", "Kern", "Kings", "Lake", "Lassen",
    "Los Angeles", "Madera", "Marin", "Mariposa", "Mendocino", "Merced", "Modoc", "Mono", "Monterey", "Napa",
    "Nevada", "Orange", "Placer", "Plumas", "Riverside", "Sacramento", "San Benito", "San Bernardino",
    "San Diego", "San Francisco", "San Joaquin", "San Luis Obispo", "San Mateo", "Santa Barbara", "Santa Clara",
    "Santa Cruz", "Shasta", "Sierra", "Siskiyou", "Solano", "Sonoma", "Stanislaus", "Sutter", "Tehama",
    "Trinity", "Tulare", "Tuolumne", "Ventura", "Yolo", "Yuba"]
COUNTY_IDS = {c: i + 1 for i, c in enumerate(CA_COUNTIES)}
LOOKUPS = {
    "San Francisco": "https://www.sf.gov/departments/department-elections",
    "Alameda": "https://acvote.alamedacountyca.gov",
    "Contra Costa": "https://www.contracostavote.gov",
    "Marin": "https://www.marincounty.gov/departments/elections",
    "Napa": "https://www.countyofnapa.org/385/Elections",
    "San Mateo": "https://www.smcacre.gov/elections",
    "Santa Clara": "https://vote.santaclaracounty.gov",
    "Solano": "https://www.solanocounty.gov/depts/rov",
    "Sonoma": "https://sonomacounty.gov/administrative-support-and-fiscal-services/registrar-of-voters",
}

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_lock = threading.Lock()
_session_at = 0.0
_cache = {}

def _get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with _opener.open(req, timeout=timeout) as r:
        return r.read()

def _ensure_session():
    global _session_at
    with _lock:
        if time.time() - _session_at > 600:
            _get(SOS + "/")
            _session_at = time.time()

def county_for(address):
    """Census geocoder: returns {county, state, matched, lat, lon} for any U.S. address, or None."""
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current",
                                "vintage": "Current_Current", "layers": "Counties", "format": "json"})
    d = json.loads(_get("https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress?" + q))
    m = d.get("result", {}).get("addressMatches", [])
    if not m:
        return None
    m = m[0]
    cs = m.get("geographies", {}).get("Counties") or [{}]
    name = re.sub(r" (County|Parish|Borough|Census Area|Municipality|city and borough)$", "", cs[0].get("NAME", ""))
    comp = m.get("addressComponents", {})
    return {"county": name or None, "state": comp.get("state"), "matched": m.get("matchedAddress"),
            "lat": m["coordinates"]["y"], "lon": m["coordinates"]["x"]}

def _miles(a, b, c, d):
    if None in (a, b, c, d): return None
    r = 3958.8; p1, p2 = math.radians(a), math.radians(c)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(h)), 1)

def civic(address, home):
    """Google Civic Information API voterinfo: official polling places, early voting and drop boxes nationwide."""
    q = urllib.parse.urlencode({"address": address, "key": CIVIC_KEY})
    try:
        d = json.loads(_get("https://www.googleapis.com/civicinfo/v2/voterinfo?" + q, timeout=20))
    except urllib.error.HTTPError as e:
        try: msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception: msg = ""
        return None, msg
    out, seen = [], set()
    def add(kind, loc):
        a = loc.get("address", {})
        k = (a.get("line1", "").lower(), kind)
        if k in seen: return
        seen.add(k)
        hrs = (loc.get("pollingHours") or "").strip()
        label = {"poll": "Election Day", "early": "Early voting", "drop": "Ballot drop-off"}[kind]
        lat, lon = loc.get("latitude"), loc.get("longitude")
        out.append({"name": a.get("locationName") or label, "address": ", ".join(x for x in [a.get("line1"), a.get("line2")] if x),
                    "city": a.get("city", ""), "state": a.get("state", ""), "zip": a.get("zip", ""),
                    "hours": f"{label}: {hrs}" if hrs else label, "kind": kind,
                    "dropOff": kind == "drop", "sameDayRegistration": False, "notes": loc.get("notes", ""),
                    "lat": lat, "lon": lon, "miles": _miles(home.get("lat"), home.get("lon"), lat, lon) if home else None})
    for loc in d.get("pollingLocations", []): add("poll", loc)
    for loc in d.get("earlyVoteSites", []): add("early", loc)
    for loc in d.get("dropOffLocations", []): add("drop", loc)
    st = (d.get("state") or [{}])[0]
    body = st.get("electionAdministrationBody", {})
    link = body.get("votingLocationFinderUrl") or body.get("electionInfoUrl")
    return {"results": out, "link": link, "election": d.get("election", {}).get("name")}, None

def vote_centers(street, city, zip_):
    _ensure_session()
    q = urllib.parse.urlencode({"SearchAddress": street, "SearchCity": city, "SearchZip": zip_,
                                "IsEarlyVoting": "true", "IsDropOffLocation": "false",
                                "IsDefaultSearch": "false", "IsReset": "false"})
    raw = _get(SOS + "/Home/GetVoteCenters?" + q,
               {"X-Requested-With": "XMLHttpRequest", "Referer": SOS + "/"})
    return json.loads(raw).get("searchResults", [])

_hood_cache = {}
def neighborhood(lat, lon):
    if lat is None: return None
    k = (round(lat, 4), round(lon, 4))
    if k not in _hood_cache:
        try:
            d = json.loads(_get(f"https://photon.komoot.io/reverse?lat={lat}&lon={lon}&lang=en&limit=1", timeout=8))
            pr = (d.get("features") or [{}])[0].get("properties", {})
            _hood_cache[k] = {"name": pr.get("district") or pr.get("locality"), "city": pr.get("city")}
        except Exception:
            return None
    return _hood_cache[k]

def _ca_rows(street, city, zip_, county):
    allrows = sorted(vote_centers(street, city, zip_), key=lambda r: r.get("distanceToVoteCenter") or 999)
    if county is None and allrows:
        ids = {v: k for k, v in COUNTY_IDS.items()}
        county = ids.get(allrows[0].get("countyID"))
    rows = [r for r in allrows if county in COUNTY_IDS and r.get("countyID") == COUNTY_IDS[county]]
    return county, [{
        "name": re.sub(r"\s+", " ", r["voteCenterName"]).strip(),
        "address": ", ".join(x for x in [r.get("address1"), r.get("address2")] if x),
        "city": r.get("city", ""), "state": "CA", "zip": r.get("zipCode", ""),
        "miles": r.get("distanceToVoteCenter"),
        "hours": r.get("operationPeriod", ""), "kind": "center",
        "dropOff": bool(r.get("isDropOffLocation")),
        "sameDayRegistration": bool(r.get("isCVRAvailable")),
        "notes": r.get("locationNotes", ""),
        "lat": float(r["latitude"]) if r.get("latitude") else None,
        "lon": float(r["longitude"]) if r.get("longitude") else None,
    } for r in rows[:8]]

def lookup(params):
    street = params.get("street", "").strip()
    city = params.get("city", "").strip()
    state = params.get("state", "").strip().upper()
    zip_ = params.get("zip", "").strip()
    key = (street.lower(), city.lower(), state, zip_)
    if key in _cache and time.time() - _cache[key][0] < 900:
        return _cache[key][1]
    full = ", ".join(x for x in [street, city, f"{state} {zip_}".strip()] if x)
    try:
        home = county_for(full)
    except Exception:
        home = None
    if home:
        state = home.get("state") or state
    if not home and not state:
        return {"ok": False, "error": "address_not_found"}
    county = (home or {}).get("county")
    out = {"ok": True, "county": county, "state": state, "matchedAddress": (home or {}).get("matched"),
           "countyLookup": LOOKUPS.get(county) if state == "CA" else None, "results": [], "source": None}
    civ_err = None
    if CIVIC_KEY:
        res, civ_err = civic((home or {}).get("matched") or full, home)
        if res and res["results"]:
            res["results"].sort(key=lambda r: (r["kind"] != "poll", r["miles"] if r["miles"] is not None else 999))
            out.update(results=res["results"][:10], source="civic", election=res["election"])
            out["countyLookup"] = out["countyLookup"] or res["link"]
    if not out["results"] and state == "CA":
        try:
            county, rows = _ca_rows(street, city, zip_, county)
            out.update(county=county, results=rows, source="ca_sos")
            out["countyLookup"] = out["countyLookup"] or LOOKUPS.get(county)
        except Exception:
            pass
    if not out["results"]:
        out["error"] = "no_data"
        out["needsKey"] = not CIVIC_KEY
        out["civicError"] = civ_err
    if out["results"]:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            hoods_ = list(ex.map(lambda x: neighborhood(x["lat"], x["lon"]), out["results"]))
        for x, hd in zip(out["results"], hoods_):
            x["neighborhood"] = (hd or {}).get("name")
            x["hoodCity"] = (hd or {}).get("city")
    _cache[key] = (time.time(), out)
    return out

import math
_access_cache = {}
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

def _meters(a, b, c, d):
    r = 6371000; p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    x = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(x))

STOP = {"street", "center", "centre", "school", "community", "voting", "avenue", "hall", "church", "united", "building", "room", "park", "library", "senior", "elementary", "middle", "high", "public", "berkeley", "oakland", "francisco", "jose", "office", "main", "city"}
def _words(s):
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower()) if w not in STOP}

def access_for(points):
    """points: list of (lat, lon, name). Returns one dict per point, built from OpenStreetMap tags."""
    todo = [pt for pt in points if (round(pt[0], 5), round(pt[1], 5)) not in _access_cache]
    if todo:
        las = [pt[0] for pt in todo]; los = [pt[1] for pt in todo]
        bb = f"{min(las)-0.009},{min(los)-0.011},{max(las)+0.009},{max(los)+0.011}"
        q = (f"[out:json][timeout:25][bbox:{bb}];(nwr[wheelchair];nwr[\"toilets:wheelchair\"];nwr[amenity=parking];nwr[amenity=bicycle_parking];"
             "node[highway=bus_stop];nwr[railway=station];nwr[amenity=toilets];node[amenity=bench];nwr[amenity=drinking_water];);out center tags;way[highway~\"^(motorway|trunk|primary|secondary)$\"];out geom;")
        data = None
        for url in OVERPASS + OVERPASS:
            try:
                req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(),
                                             headers={"User-Agent": "SwayWay/0.1 (local prototype)", "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read()); break
            except Exception:
                time.sleep(1.5)
                continue
        if data is None:
            raise RuntimeError("accessibility data unavailable")
        els, roads = [], []
        for e in data.get("elements", []):
            if e.get("type") == "way" and e.get("geometry") and (e.get("tags") or {}).get("highway"):
                roads.append([(g["lat"], g["lon"]) for g in e["geometry"]]); continue
            la = e.get("lat") or (e.get("center") or {}).get("lat"); lo = e.get("lon") or (e.get("center") or {}).get("lon")
            if la is not None: els.append((la, lo, e.get("tags", {})))
        for lat, lon, name in todo:
            near = [(_meters(lat, lon, a, b), t) for a, b, t in els]
            kind = lambda t: "toilet" if t.get("amenity") == "toilets" else "bench" if t.get("amenity") == "bench" else "water" if t.get("amenity") == "drinking_water" else "bike" if t.get("amenity") == "bicycle_parking" else "parking" if t.get("amenity") == "parking" else "bus" if t.get("highway") == "bus_stop" else "rail" if t.get("railway") == "station" else "place"
            places = sorted([(m, t) for m, t in near if kind(t) == "place" and m <= 90], key=lambda x: x[0])
            nw = _words(name)
            match = next(((m, t) for m, t in places if nw & _words(t.get("name"))), None) or next(((m, t) for m, t in places if m <= 40), None)
            wc = match[1].get("wheelchair") if match else None
            toilets = match[1].get("toilets:wheelchair") if match else None
            parking = sorted([(m, t) for m, t in near if kind(t) == "parking" and m <= 250], key=lambda x: x[0])
            bike = sorted([(m, t) for m, t in near if kind(t) == "bike" and m <= 150], key=lambda x: x[0])
            def seg(lat0, lon0, a, b):
                kx = 111320 * math.cos(math.radians(lat0)); ky = 110540
                ax, ay = (a[1]-lon0)*kx, (a[0]-lat0)*ky; bx, by = (b[1]-lon0)*kx, (b[0]-lat0)*ky
                dx, dy = bx-ax, by-ay; L = dx*dx+dy*dy
                tt = 0 if L == 0 else max(0, min(1, -(ax*dx+ay*dy)/L))
                return math.hypot(ax+tt*dx, ay+tt*dy)
            road_m = min((seg(lat, lon, g[i], g[i+1]) for g in roads for i in range(len(g)-1)), default=None)
            near_of = lambda k, r: min((m for m, t in near if kind(t) == k and m <= r), default=None)
            bus = sorted([(m, t) for m, t in near if kind(t) == "bus" and m <= 450], key=lambda x: x[0])
            rail = sorted([(m, t) for m, t in near if kind(t) == "rail" and m <= 900], key=lambda x: x[0])
            _access_cache[(round(lat, 5), round(lon, 5))] = {
                "wheelchair": wc if wc in ("yes", "limited", "no") else None,
                "wheelchairSource": (match[1].get("name") if match else None),
                "accessibleToilets": toilets if toilets in ("yes", "no") else None,
                "parking": bool(parking),
                "restroom": near_of("toilet", 150) is not None,
                "seating": near_of("bench", 100) is not None,
                "water": near_of("water", 150) is not None,
                "busyRoadMeters": round(road_m) if road_m is not None else None,
                "bike": {"meters": round(bike[0][0])} if bike else None,
                "accessibleParking": any(t.get("wheelchair") == "yes" or t.get("capacity:disabled", "0") not in ("0", "no") for _, t in parking),
                "bus": {"name": bus[0][1].get("name"), "meters": round(bus[0][0])} if bus else None,
                "rail": {"name": rail[0][1].get("name"), "network": rail[0][1].get("network"), "meters": round(rail[0][0])} if rail else None,
            }
    return [_access_cache[(round(la, 5), round(lo, 5))] for la, lo, _ in points]

# ---------- Routing (Valhalla, OpenStreetMap) ----------
VALHALLA = "https://valhalla1.openstreetmap.de/route"
_hoods = None
def hoods():
    global _hoods
    if _hoods is None:
        s = open(os.path.join(ROOT, "hoods.js"), encoding="utf-8").read()
        _hoods = {(n + "|" + c).lower(): (n, c, la, lo, r) for n, c, la, lo, r in json.loads(s[s.index("["):s.rindex("]") + 1])}
    return _hoods

def geocode(addr):
    q = urllib.parse.urlencode({"address": addr, "benchmark": "Public_AR_Current", "format": "json"})
    try:
        d = json.loads(_get("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?" + q))
        m = d["result"]["addressMatches"]
        if m: return m[0]["coordinates"]["y"], m[0]["coordinates"]["x"]
    except Exception:
        pass
    d = json.loads(_get("https://photon.komoot.io/api/?q=" + urllib.parse.quote(addr) + "&limit=1&lang=en"))
    f = d.get("features") or []
    if f:
        lo, la = f[0]["geometry"]["coordinates"]; return la, lo
    return None

def costing_for(vehicle, mode):
    v = (vehicle or "").lower()
    import re as _re
    if _re.search(r"wheelchair|stroller", v): return "pedestrian", {"pedestrian": {"type": "wheelchair"}}, "step-free walking route"
    if _re.search(r"motorcycle|dirt bike|boda", v): return "motorcycle", {}, "motorcycle route"
    if _re.search(r"moped|three-wheeled|auto-rickshaw|tuk", v): return "motor_scooter", {}, "scooter and moped route"
    if _re.search(r"truck|lorry|semi|tractor-trailer|freight|delivery van|fire engine|forklift", v): return "truck", {}, "truck route"
    if _re.search(r"^bus$|minibus|coach bus|school bus|chicken bus|chiva", v): return "bus", {}, "bus-legal route"
    if _re.search(r"taxi|rideshare|limousine|cab", v): return "taxi", {}, "taxi route"
    if _re.search(r"carpool", v): return "auto", {"auto": {"include_hov2": True}}, "carpool route using HOV lanes"
    if _re.search(r"mountain bike", v): return "bicycle", {"bicycle": {"bicycle_type": "Mountain"}}, "mountain bike route"
    if _re.search(r"scooter|skate|roller|inline|longboard", v): return "bicycle", {"bicycle": {"bicycle_type": "City", "use_roads": 0.2}}, "bike-lane route"
    if mode == "walking": return "pedestrian", {}, "walking route"
    if mode == "bicycling": return "bicycle", {}, "bike route"
    if mode == "driving": return "auto", {}, "driving route"
    return None, None, None

def circle(la, lo, r, n=20):
    pts = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        pts.append([round(lo + (r / (111320 * math.cos(math.radians(la)))) * math.cos(a), 6), round(la + (r / 110540) * math.sin(a), 6)])
    return pts

def decode6(s):
    coords, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        for which in (0, 1):
            shift = res = 0
            while True:
                b = ord(s[i]) - 63; i += 1; res |= (b & 0x1f) << shift; shift += 5
                if b < 0x20: break
            d = ~(res >> 1) if res & 1 else res >> 1
            if which == 0: lat += d
            else: lon += d
        coords.append([lat / 1e6, lon / 1e6])
    return coords

def valhalla(costing, opts, a, b, polys):
    import subprocess
    req = {"locations": [{"lat": a[0], "lon": a[1]}, {"lat": b[0], "lon": b[1]}], "costing": costing,
           "costing_options": opts, "directions_options": {"units": "miles"}}
    if polys: req["exclude_polygons"] = polys
    pr = subprocess.run(["curl", "-s", "--max-time", "25", "-A", "SwayWay/0.1 (local prototype)", "-H", "Content-Type: application/json",
                         "-d", json.dumps(req), "-w", "\n%{http_code}", VALHALLA], capture_output=True, text=True)
    out_txt, _, status = pr.stdout.rpartition("\n")
    try: d = json.loads(out_txt or "{}")
    except Exception: d = {}
    if status != "200" or "trip" not in d: return None
    leg = d["trip"]["legs"][0]; s = d["trip"]["summary"]
    return {"shape": decode6(leg["shape"]), "miles": round(s["length"], 2), "minutes": max(1, round(s["time"] / 60)),
            "steps": [m["instruction"] for m in leg["maneuvers"]]}

RANK = {"driving": 3, "bicycling": 2, "walking": 1}
TAIL = {"walking": 400, "bicycling": 2000}
STOPS = [(r"ferry|water taxi", "amenity:ferry_terminal", "ferry terminal"),
         (r"bus|coach|matatu|jeepney|chicken|chiva|dolmu|marshrutka|tro-tro|daladala|angkot|bemo|songthaew", "highway:bus_stop", "bus stop"),
         (r".", "railway:station", "station")]

def _re_bus(v):
    import re as _re
    return bool(_re.search(STOPS[1][0], v.lower()))

def find_stop(pt, vname):
    import re as _re
    tags = [s[1] for s in STOPS if _re.search(s[0], vname.lower())][:1]
    if tags and tags[0] == "railway:station": tags.append("highway:bus_stop")
    for tag in tags:
        try:
            d = json.loads(_get(f"https://photon.komoot.io/reverse?lat={pt[0]}&lon={pt[1]}&osm_tag={tag}&radius=3&limit=5&lang=en", timeout=10))
        except Exception:
            continue
        best = None
        for f in d.get("features", []):
            lo, la = f["geometry"]["coordinates"]; m = _meters(pt[0], pt[1], la, lo)
            if m <= 3000 and (best is None or m < best[0]): best = (m, la, lo, f["properties"].get("name") or "the stop")
        if best: return best
    return None

def point_at(shape, dist_from_start):
    acc = 0
    for i in range(len(shape) - 1):
        seg = _meters(shape[i][0], shape[i][1], shape[i+1][0], shape[i+1][1])
        if acc + seg >= dist_from_start: return tuple(shape[i+1])
        acc += seg
    return tuple(shape[-1])

def shape_len(shape):
    return sum(_meters(shape[i][0], shape[i][1], shape[i+1][0], shape[i+1][1]) for i in range(len(shape) - 1))


TRANSITOUS = "https://api.transitous.org/api/v1/plan"
TIMES_UTC = {"morning": "2026-11-03T17:00:00Z", "midday": "2026-11-03T21:00:00Z", "evening": "2026-11-04T01:30:00Z"}
TMODES = [(r"ferry|water taxi", "FERRY"), (r"subway|metro|underground|bart", "SUBWAY,METRO"), (r"tram|streetcar|light rail", "TRAM"),
          (r"cable car", "CABLE_CAR"), (r"funicular", "FUNICULAR"), (r"gondola|aerial", "AERIAL_LIFT"),
          (r"high-speed|bullet", "HIGHSPEED_RAIL,RAIL"), (r"train|rail|commuter|intercity|caltrain|amtrak", "RAIL,REGIONAL_RAIL,REGIONAL_FAST_RAIL,LONG_DISTANCE,SUBWAY"),
          (r"bus|coach|matatu|jeepney|chicken|chiva|dolmu|marshrutka|tro-tro|daladala|angkot|bemo|songthaew", "BUS,COACH")]
MODE_NAME = {"BUS": "Bus", "COACH": "Bus", "SUBWAY": "Subway", "METRO": "Subway", "TRAM": "Tram", "FERRY": "Ferry", "RAIL": "Train",
             "REGIONAL_RAIL": "Train", "REGIONAL_FAST_RAIL": "Train", "LONG_DISTANCE": "Train", "HIGHSPEED_RAIL": "Train", "CABLE_CAR": "Cable car",
             "FUNICULAR": "Funicular", "AERIAL_LIFT": "Gondola lift", "WALK": "Walk", "BIKE": "Bike", "CAR": "Drive"}

def decode_poly(s, precision=6):
    coords, i, lat, lon, f = [], 0, 0, 0, 10 ** precision
    while i < len(s):
        for which in (0, 1):
            shift = res = 0
            while True:
                b = ord(s[i]) - 63; i += 1; res |= (b & 0x1f) << shift; shift += 5
                if b < 0x20: break
            d = ~(res >> 1) if res & 1 else res >> 1
            if which == 0: lat += d
            else: lon += d
        coords.append([lat / f, lon / f])
    return coords

def local_time(iso):
    import datetime
    t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")) - datetime.timedelta(hours=8)
    return t.strftime("%-I:%M %p")

def transit_route(start, end, picks, when):
    import re as _re, subprocess
    tnames = [pk["v"] for pk in picks if pk["m"] == "transit"]
    modes = set()
    for v in tnames:
        for rx, m in TMODES:
            if _re.search(rx, v.lower()): modes.update(m.split(",")); break
        else: modes.add("TRANSIT")
    names = [pk["v"].lower() for pk in picks]
    access = "WALK"
    if any(pk["m"] == "bicycling" for pk in picks): access = "BIKE"
    if any(pk["m"] == "driving" for pk in picks): access = "CAR"
    wheel = any(_re.search(r"wheelchair|stroller", n) for n in names)
    base = {"fromPlace": f"{start[0]},{start[1]}", "toPlace": f"{end[0]},{end[1]}", "time": TIMES_UTC.get(when, TIMES_UTC["midday"]), "numItineraries": "3"}
    if wheel: base["pedestrianProfile"] = "WHEELCHAIR"
    tries = [dict(base, transitModes=",".join(sorted(modes)), preTransitModes=access, postTransitModes="WALK"),
             dict(base, transitModes=",".join(sorted(modes))),
             dict(base, transitModes="TRANSIT")]
    for ti, q in enumerate(tries):
        pr = subprocess.run(["curl", "-s", "--max-time", "25", "-A", "SwayWay/0.1 (local prototype)", TRANSITOUS + "?" + urllib.parse.urlencode(q)], capture_output=True, text=True)
        try: d = json.loads(pr.stdout or "{}")
        except Exception: d = {}
        its = [it for it in d.get("itineraries", []) if any(l["mode"] not in ("WALK", "BIKE", "CAR") for l in it["legs"])]
        if its: break
    else:
        return None
    it = min(its, key=lambda x: x["duration"])
    notes = []
    used = {MODE_NAME.get(l["mode"], "") for l in it["legs"]}
    if ti > 0 and modes != {"TRANSIT"}:
        notes.append("No route used only " + " and ".join(tnames).lower() + " at that time, so this plan mixes in other transit.")
    if access == "BIKE" and "Bike" not in used: notes.append("The transit planner couldn't fit a bike leg, so the trip to the station is shown as walking.")
    if access == "CAR" and "Drive" not in used: notes.append("The transit planner couldn't fit a drive to the station, so that part is shown as walking.")
    legs = []
    for l in it["legs"]:
        g = l.get("legGeometry") or {}
        shape = decode_poly(g.get("points", ""), g.get("precision", 6)) if g.get("points") else [[l["from"]["lat"], l["from"]["lon"]], [l["to"]["lat"], l["to"]["lon"]]]
        kind = MODE_NAME.get(l["mode"], l["mode"].title())
        frm = l["from"].get("name"); to = l["to"].get("name")
        frm = "your start" if frm == "START" else frm; to = "your polling place" if to == "END" else to
        if l["mode"] in ("WALK", "BIKE", "CAR"):
            if wheel and l["mode"] == "WALK": kind = "Wheelchair" if any("wheelchair" in n for n in names) else "Stroller"
            steps = []
            for s in l.get("steps") or []:
                rd = (s.get("relativeDirection") or "").replace("_", " ").lower(); st = s.get("streetName") or "the path"
                if s.get("distance", 0) < 5: continue
                steps.append(("Continue on " if rd in ("continue", "depart", "") else f"{rd.capitalize()} onto ") + f"{st} ({round(s['distance'])} m)")
            label = f"to {to}"
        else:
            route = l.get("routeShortName") or l.get("displayName") or ""
            agency = l.get("agencyName") or ""
            label = f"{agency + ' ' if agency else ''}{route} · {frm} to {to}".strip()
            steps = [f"Board {agency + ' ' if agency else ''}{kind.lower()} {route} at {frm} ({local_time(l['startTime'])}). Get off at {to} ({local_time(l['endTime'])})."]
        legs.append({"name": kind, "mode": "transit" if l["mode"] not in ("WALK", "BIKE", "CAR") else l["mode"].lower(), "label": label,
                     "shape": shape, "miles": round(l.get("distance", 0) / 1609, 2) if l.get("distance") else round(shape_len(shape) / 1609, 2),
                     "minutes": max(1, round(l.get("duration", 0) / 60)), "steps": steps,
                     "depart": local_time(l["startTime"]), "arrive": local_time(l["endTime"])})
    legs = [x for x in legs if not (x["mode"] == "walk" and x["minutes"] <= 1 and x["miles"] < 0.03)]
    return {"legs": legs, "notes": notes, "minutes": round(it["duration"] / 60), "depart": local_time(it["startTime"]), "arrive": local_time(it["endTime"])}

def route(body):
    start = geocode(body.get("from", ""))
    if not start: return {"ok": False, "error": "start_not_found"}
    end = (float(body["toLat"]), float(body["toLon"]))
    picks = body.get("picks") or [{"v": body.get("vehicle") or "Walk", "m": body.get("mode", "walking")}]
    hd = hoods(); avoided, skipped, polys = [], [], []
    minla, maxla = min(start[0], end[0]) - 0.03, max(start[0], end[0]) + 0.03
    minlo, maxlo = min(start[1], end[1]) - 0.04, max(start[1], end[1]) + 0.04
    for k in body.get("avoid", []):
        if k not in hd: continue
        n, c, la, lo, r = hd[k]
        if not (minla <= la <= maxla and minlo <= lo <= maxlo): continue
        if _meters(la, lo, *start) <= r or _meters(la, lo, *end) <= r: skipped.append(n); continue
        avoided.append({"name": n, "city": c, "lat": la, "lon": lo, "r": r}); polys.append(circle(la, lo, r))
    W = {"driving": 6, "bicycling": 3, "walking": 1}
    transit_picks = [pk for pk in picks if pk["m"] == "transit"]
    nt = sorted([pk for pk in picks if pk["m"] != "transit"], key=lambda pk: -RANK[pk["m"]])
    def leg(pk, a, b):
        costing, opts, label = costing_for(pk["v"], pk["m"])
        r = valhalla(costing, opts, a, b, polys)
        if r: r.update({"name": pk["v"], "mode": pk["m"], "label": label})
        return r
    def chain(seq, a, b):
        """Route a->b using every pick in seq, each over its own stretch (fastest first)."""
        if len(seq) == 1:
            r = leg(seq[0], a, b); return [r] if r else None
        base = leg(seq[0], a, b)
        if not base: return None
        total = shape_len(base["shape"]); s = sum(W[pk["m"]] for pk in seq); acc = 0; cuts = []
        for pk in seq[:-1]:
            acc += W[pk["m"]]; cuts.append(point_at(base["shape"], total * acc / s))
        pts = [a] + cuts + [b]; out = []
        for i, pk in enumerate(seq):
            r = leg(pk, pts[i], pts[i + 1])
            if not r: return None
            out.append(r)
        return out
    notes = []
    if transit_picks:
        tr = transit_route(start, end, picks, body.get("time", "midday"))
        if not tr: return {"ok": True, "fallback": "transit", "start": start, "end": end}
        legs = tr["legs"]; notes = list(tr["notes"])
        access = [pk for pk in nt]
        if access:
            first_i = 0 if legs and legs[0]["mode"] != "transit" else None
            last_i = len(legs) - 1 if legs and legs[-1]["mode"] != "transit" else None
            if first_i is None and last_i is None: head, tail = [], []
            elif first_i is None: head, tail = [], access
            elif last_i is None or first_i == last_i: head, tail = access, []
            elif len(access) > 1: head, tail = access[:-1], access[-1:]
            else: head, tail = access, []
            if tail:
                lg = legs[last_i]; r = chain(tail, tuple(lg["shape"][0]), tuple(lg["shape"][-1]))
                if r: legs = legs[:last_i] + r + legs[last_i + 1:]
            if head and first_i is not None:
                lg = legs[first_i]; r = chain(head, tuple(lg["shape"][0]), tuple(lg["shape"][-1]))
                if r: legs = r + legs[1:]
        used = {l["name"].lower() for l in legs}
        notes = [n for n in notes if not (("bike leg" in n and any(l["mode"] == "bicycling" for l in legs)) or ("drive to the station" in n and any(l["mode"] == "driving" for l in legs)))]
        tmap = {"bus": "bus", "subway": "subway", "train": "train", "tram": "tram", "ferry": "ferry"}
        kinds = {l["name"].lower() for l in legs if l["mode"] == "transit"}
        alias = {"train": {"train", "subway", "tram"}, "subway": {"subway", "train"}, "metro": {"subway", "train"}, "light rail": {"tram", "train"}, "streetcar": {"tram"}}
        def served(v):
            v = v.lower()
            for key, ks in alias.items():
                if key in v and kinds & ks: return True
            return any(k in v or v in k for k in kinds)
        missing = [pk["v"] for pk in transit_picks if pk["v"] != "Transit" and not served(pk["v"])]
        if missing: notes.append("No " + " or ".join(m.lower() for m in missing) + " service fits this trip on Nov 3, so it isn't on the map.")
        return {"ok": True, "start": start, "end": end, "legs": legs, "avoided": avoided, "skipped": skipped, "transit": True,
                "avoidIgnored": bool(polys) or bool(skipped), "notes": notes, "depart": tr["depart"], "arrive": tr["arrive"],
                "miles": round(sum(l["miles"] for l in legs), 1), "minutes": max(tr["minutes"], sum(l["minutes"] for l in legs))}
    legs = chain(nt, start, end)
    if not legs: return {"ok": False, "error": "no_route"}
    return {"ok": True, "start": start, "end": end, "legs": legs, "avoided": avoided, "skipped": skipped, "notes": notes,
            "miles": round(sum(l["miles"] for l in legs), 1), "minutes": sum(l["minutes"] for l in legs)}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/vote-centers":
            params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                body, code = json.dumps(lookup(params)).encode(), 200
            except Exception as e:
                body, code = json.dumps({"ok": False, "error": "lookup_failed", "detail": str(e)[:200]}).encode(), 502
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/access":
            params = urllib.parse.parse_qs(u.query)
            try:
                pts = json.loads(params.get("pts", ["[]"])[0])
                body, code = json.dumps({"ok": True, "results": access_for([(float(a), float(b), str(n)) for a, b, n in pts][:10])}).encode(), 200
            except Exception as e:
                body, code = json.dumps({"ok": False, "error": str(e)[:200]}).encode(), 502
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if u.path.startswith("/.build") or u.path.endswith(".py"):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/api/route":
            self.send_error(404); return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            out, code = route(body), 200
        except Exception as e:
            out, code = {"ok": False, "error": "route_failed", "detail": str(e)[:200]}, 502
        data = json.dumps(out).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
