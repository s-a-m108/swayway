"""SwayWay local server: serves the app and looks up official vote centers.

Vote centers come from the California Secretary of State's early voting /
vote center lookup (caearlyvoting.sos.ca.gov). The voter's county comes from
the U.S. Census Bureau geocoder, so results stay inside the voter's county.
"""
import http.cookiejar, json, os, re, threading, time, urllib.parse, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8642))
HOST = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
SOS = "https://caearlyvoting.sos.ca.gov"
COUNTY_IDS = {"Alameda": 1, "Contra Costa": 7, "Marin": 21, "Napa": 28, "San Francisco": 38,
              "San Mateo": 41, "Santa Clara": 43, "Solano": 48, "Sonoma": 49}
# Each county's official lookup, as linked from https://www.sos.ca.gov/elections/polling-place
LOOKUPS = {
    "Alameda": "https://acgov.org/rovapps/maps/vca/",
    "Contra Costa": "https://www.contracostavote.gov/elections/election-day-polling-places/",
    "Marin": "https://www.marincounty.gov/departments/elections",
    "Napa": "https://www.napacounty.gov/2354/Vote-Center-Locations",
    "San Francisco": "https://www.sf.gov/vote-in-person",
    "San Mateo": "https://smcacre.gov/elections/vote-center-and-ballot-drop-box-lookup",
    "Santa Clara": "https://rovservices.sccgov.org/Home/IndexPost?selected=vc",
    "Solano": "https://www.solanocounty.gov/government/registrar-voters/sample-ballotpolling-place-lookup",
    "Sonoma": "https://sonomacounty.gov/administrative-support-and-fiscal-services/registrar-of-voters/elections/where-to-vote-or-drop-off-your-ballot",
}
# Counties that still assign each voter one precinct polling place (not Voter's Choice Act vote-center counties)
PRECINCT_COUNTIES = {"San Francisco", "Contra Costa", "Solano"}

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
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current",
                                "vintage": "Current_Current", "layers": "Counties", "format": "json"})
    d = json.loads(_get("https://geocoding.geo.census.gov/geocoder/geographies/onelineaddress?" + q))
    m = d.get("result", {}).get("addressMatches", [])
    if not m:
        return None, None
    name = m[0]["geographies"]["Counties"][0]["NAME"].replace(" County", "")
    return name, m[0]["matchedAddress"]

def vote_centers(street, city, zip_, dropoff=False):
    _ensure_session()
    q = urllib.parse.urlencode({"SearchAddress": street, "SearchCity": city, "SearchZip": zip_,
                                "IsEarlyVoting": "false" if dropoff else "true", "IsDropOffLocation": "true" if dropoff else "false",
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

MONTHS = {m: i + 1 for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                           "September", "October", "November", "December"])}
def site_kinds(hours, from_dropoff_query, is_dropoff):
    """Election Day site, early voting site, ballot drop-off, from the SoS operationPeriod text."""
    if from_dropoff_query: return {"drop"}
    kinds = {"drop"} if is_dropoff else set()
    eday, early = False, False
    for seg in (hours or "").split(";"):
        m = re.match(r"^(.*?):\s*(\d.*)$", seg.strip())
        if not m: continue
        d = m.group(1).strip()
        if re.match(r"election day", d, re.I): eday = True; continue
        for mo, day in re.findall(r"([A-Z][a-z]+)\s+(\d+)", d):
            n = MONTHS.get(mo)
            if not n: continue
            if (n, int(day)) == (11, 3): eday = True
            elif (n, int(day)) < (11, 3): early = True
    if not hours or not (eday or early): eday = True
    if eday: kinds.add("eday")
    if early: kinds.add("early")
    return kinds

def lookup(params):
    street = params.get("street", "").strip()
    city = params.get("city", "").strip()
    zip_ = params.get("zip", "").strip()
    key = (street.lower(), city.lower(), zip_)
    if key in _cache and time.time() - _cache[key][0] < 900:
        return _cache[key][1]
    try:
        county, matched = county_for(f"{street}, {city}, CA {zip_}".strip())
    except Exception:
        county, matched = None, None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as ex:
        fv = ex.submit(vote_centers, street, city, zip_, False)
        fd = ex.submit(vote_centers, street, city, zip_, True)
        vrows = fv.result()
        try: drows = fd.result()
        except Exception: drows = []
    if county is None and (vrows or drows):
        ids = {v: k for k, v in COUNTY_IDS.items()}
        county = ids.get((vrows or drows)[0].get("countyID"))
    if county not in COUNTY_IDS:
        out = {"ok": False, "error": "outside_bay" if county else "address_not_found", "county": county}
        _cache[key] = (time.time(), out)
        return out
    sites = {}
    for rows, from_drop in ((vrows, False), (drows, True)):
        for r in rows:
            if r.get("countyID") != COUNTY_IDS[county]: continue
            name = re.sub(r"\s+", " ", r.get("voteCenterName") or "").strip()
            addr = ", ".join(x for x in [r.get("address1"), r.get("address2")] if x)
            k = (name.lower(), addr.lower())
            kinds = site_kinds(r.get("operationPeriod", ""), from_drop, bool(r.get("isDropOffLocation")))
            if k in sites:
                sites[k]["kinds"] = sorted(set(sites[k]["kinds"]) | kinds)
                if from_drop and r.get("operationPeriod"): sites[k]["dropHours"] = r.get("operationPeriod")
                continue
            sites[k] = {"name": name, "address": addr, "city": r.get("city", ""), "zip": r.get("zipCode", ""),
                        "miles": r.get("distanceToVoteCenter"),
                        "hours": "" if from_drop else r.get("operationPeriod", ""),
                        "dropHours": r.get("operationPeriod", "") if from_drop else "",
                        "dropOff": "drop" in kinds, "sameDayRegistration": bool(r.get("isCVRAvailable")),
                        "notes": r.get("locationNotes", ""), "kinds": sorted(kinds),
                        "lat": float(r["latitude"]) if r.get("latitude") else None,
                        "lon": float(r["longitude"]) if r.get("longitude") else None}
    near = lambda x: x["miles"] if x["miles"] is not None else 999
    allsites = sorted(sites.values(), key=near)
    keep, picked = [], set()
    for kind in ("eday", "early", "drop"):          # nearest 6 of each kind
        for x in [x for x in allsites if kind in x["kinds"]][:6]:
            if id(x) not in picked: picked.add(id(x)); keep.append(x)
    keep.sort(key=near)
    out = {"ok": True, "county": county, "matchedAddress": matched, "countyLookup": LOOKUPS.get(county),
           "precinct": county in PRECINCT_COUNTIES, "source": "https://caearlyvoting.sos.ca.gov/", "results": keep}
    with ThreadPoolExecutor(8) as ex:
        hoods_ = list(ex.map(lambda x: neighborhood(x["lat"], x["lon"]), keep))
    for x, hd in zip(keep, hoods_):
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
    d = json.loads(_get("https://photon.komoot.io/api/?q=" + urllib.parse.quote(addr) + "&limit=1&lang=en&bbox=-123.65,36.89,-121.2,38.87"))
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
    d = {}
    try:
        pr = subprocess.run(["curl", "-s", "--max-time", "20", "-A", "SwayWay/0.1 (local prototype)", "-H", "Content-Type: application/json",
                             "-d", json.dumps(req), "-w", "\n%{http_code}", VALHALLA], capture_output=True, text=True, timeout=25)
        out_txt, _, status = pr.stdout.rpartition("\n")
        if status == "200": d = json.loads(out_txt or "{}")
    except Exception:
        d = {}
    if "trip" not in d:
        try:
            rq = urllib.request.Request(VALHALLA, data=json.dumps(req).encode(), headers={"Content-Type": "application/json", "User-Agent": "SwayWay/0.1"})
            with urllib.request.urlopen(rq, timeout=20) as r: d = json.loads(r.read())
        except Exception:
            return None
    if "trip" not in d: return None
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


# ---------- Special vehicles: water, air and space ----------
# Boats stay on water: paths run over a 150 m land/water grid of the Bay built from
# OpenStreetMap-derived land outlines (bay-water.json). Planes use real airports,
# rockets launch from real Bay Area airfields and climb past the Karman line (100 km).
import heapq, zlib, base64
_VCAT = None
def vehicle_category(name):
    global _VCAT
    if _VCAT is None:
        s = open(os.path.join(ROOT, "vehicles.js"), encoding="utf-8").read()
        arr = json.loads(s[s.index("["):s.index("]]") + 2])
        _VCAT = {n.lower(): c for n, c, m in arr}
    return _VCAT.get((name or "").lower())

LIFTS = r"cable car|gondola lift|aerial tramway|chairlift|ski lift|ropeway|zipline"
PLANES = r"airplane|airliner|jet|charter plane|turboprop|military aircraft|seaplane|amphibious aircraft|ultralight|microlight|agricultural|electric aircraft|glider|sailplane|air ambulance"

def special_kind(pk):
    v = pk["v"].lower(); cat = vehicle_category(pk["v"])
    if cat == "Space": return "space"
    if cat == "Air" and not re.search(LIFTS, v):
        return "plane" if re.search(PLANES, v) and not re.search(r"hang glider|paraglider", v) else "fly"
    if cat == "Water" and pk["m"] != "transit": return "water"
    return None

def speed_mph(kind, v):
    v = v.lower()
    if kind == "water":
        for rx, sp in [(r"hovercraft|hydrofoil", 35), (r"speedboat|motorboat|power catamaran|cabin cruiser|inflatable|fishing", 25),
                       (r"tug", 12), (r"windsurf|kitesurf", 12), (r"sub|submersible|underwater research", 8),
                       (r"surfboard|diver propulsion", 2), (r"yacht|sail|catamaran", 7),
                       (r"cruise|liner|cargo|container|bulk|tanker|lng|hospital|icebreaker|houseboat|riverboat|paddle steamer|passenger boat", 14)]:
            if re.search(rx, v): return sp
        return 3
    if kind == "plane": return 220 if re.search(r"airliner|passenger airplane|jet|charter|cargo airplane|military", v) else 120
    for rx, sp in [(r"balloon", 8), (r"airship|blimp|zeppelin", 35), (r"hang glider|paraglider", 20), (r"wingsuit", 100),
                   (r"autogyro|gyrocopter", 80), (r"helicopter", 120)]:
        if re.search(rx, v): return sp
    return 100

AIRPORTS = {"San Francisco International Airport (SFO)": (37.6213, -122.3790), "Oakland International Airport (OAK)": (37.7126, -122.2197),
    "San Jose Mineta International Airport (SJC)": (37.3626, -121.9290), "Palo Alto Airport": (37.4611, -122.1150),
    "San Carlos Airport": (37.5119, -122.2495), "Hayward Executive Airport": (37.6592, -122.1217), "Livermore Municipal Airport": (37.6934, -121.8204),
    "Buchanan Field, Concord": (37.9897, -122.0569), "Napa County Airport": (38.2132, -122.2807), "Charles M. Schulz Sonoma County Airport": (38.5090, -122.8128),
    "Petaluma Municipal Airport": (38.2578, -122.6056), "Gnoss Field, Novato": (38.1436, -122.5561), "Half Moon Bay Airport": (37.5134, -122.5012),
    "Reid-Hillview Airport, San Jose": (37.3329, -121.8197), "Nut Tree Airport, Vacaville": (38.3768, -121.9618)}
SPACEPORTS = {"Moffett Federal Airfield (NASA Ames)": (37.4161, -122.0490), "Alameda Point (former Naval Air Station runway)": (37.7860, -122.3170),
              "Travis Air Force Base": (38.2627, -121.9275)}

_WG = None
def water_grid():
    global _WG
    if _WG is None:
        d = json.load(open(os.path.join(ROOT, "bay-water.json")))
        d["cells"] = zlib.decompress(base64.b64decode(d["grid"])); _WG = d
    return _WG

def _rc(g, la, lo): return int((g["bbox"][3] - la) / g["dlat"]), int((lo - g["bbox"][0]) / g["dlon"])
def _ll(g, r, c): return (round(g["bbox"][3] - (r + .5) * g["dlat"], 6), round(g["bbox"][0] + (c + .5) * g["dlon"], 6))

def _nearest_water(g, la, lo, rad=12):
    r0, c0 = _rc(g, la, lo); W, H, cells = g["w"], g["h"], g["cells"]; best = None
    for want in (2, 1):
        for r in range(max(0, r0 - rad), min(H, r0 + rad + 1)):
            for c in range(max(0, c0 - rad), min(W, c0 + rad + 1)):
                if cells[r * W + c] >= want:
                    d = (r - r0) ** 2 + (c - c0) ** 2
                    if best is None or d < best[0]: best = (d, r, c)
        if best: return best[1], best[2]
    return None

def _clear(g, a, b):
    (r0, c0), (r1, c1) = a, b; n = max(abs(r1 - r0), abs(c1 - c0), 1); W, cells = g["w"], g["cells"]
    for i in range(n + 1):
        r = round(r0 + (r1 - r0) * i / n); c = round(c0 + (c1 - c0) * i / n)
        if cells[r * W + c] == 0: return False
    return True

def water_path(a, b):
    """Shortest on-water path between two shore points, as [lat, lon] points."""
    g = water_grid(); W, H, cells = g["w"], g["h"], g["cells"]
    s = _nearest_water(g, *a); t = _nearest_water(g, *b)
    if not s or not t: return None
    si, ti = s[0] * W + s[1], t[0] * W + t[1]
    dist = {si: 0.0}; prev = {}; pq = [(0.0, si)]; tr, tc = t
    steps = [(-1, 0, 1), (1, 0, 1), (0, -1, 1), (0, 1, 1), (-1, -1, 1.414), (-1, 1, 1.414), (1, -1, 1.414), (1, 1, 1.414)]
    while pq:
        f, i = heapq.heappop(pq)
        if i == ti: break
        r, c = divmod(i, W); gi = dist[i]
        for dr, dc, w in steps:
            rr, cc = r + dr, c + dc
            if not (0 <= rr < H and 0 <= cc < W): continue
            j = rr * W + cc; v = cells[j]
            if v == 0: continue
            nd = gi + w * (1 if v == 2 else 3)
            if nd < dist.get(j, 1e18):
                dist[j] = nd; prev[j] = i
                heapq.heappush(pq, (nd + math.hypot(rr - tr, cc - tc), j))
    if ti not in dist: return None
    path = [ti]
    while path[-1] != si: path.append(prev[path[-1]])
    path = [divmod(i, W) for i in reversed(path)]
    out = [path[0]]; k = 0
    while k < len(path) - 1:
        j = len(path) - 1
        while j > k + 1 and not _clear(g, path[k], path[j]): j -= 1
        out.append(path[j]); k = j
    return [list(a)] + [list(_ll(g, r, c)) for r, c in out] + [list(b)]

def _km(p, q): return _meters(p[0], p[1], q[0], q[1]) / 1000

def _nearest_site(sites, pt, exclude=None):
    c = [(n, ll) for n, ll in sites.items() if n != exclude]
    return min(c, key=lambda x: _km(pt, x[1]))

def _arc(a, b, bend=0.08, n=32):
    dla, dlo = b[0] - a[0], b[1] - a[1]
    return [[a[0] + dla * t + dlo * bend * math.sin(math.pi * t), a[1] + dlo * t - dla * bend * math.sin(math.pi * t)] for t in [i / n for i in range(n + 1)]]

def _space_shape(a, b, n=48):
    out = []
    for i in range(n + 1):
        t = i / n; h = math.sin(math.pi * t)
        out.append([a[0] + (b[0] - a[0]) * t + 0.09 * math.sin(2 * math.pi * t), a[1] + (b[1] - a[1]) * t - 0.32 * h])
    return out

def site_pair(kind, here, target):
    """Pick departure/arrival sites that keep the ground travel on both ends shortest."""
    if kind == "fly": return ("your start", here, "your polling place", target)
    sites = {"water": lambda: {n: tuple(ll) for n, ll in water_grid()["launches"].items()}, "plane": lambda: AIRPORTS, "space": lambda: SPACEPORTS}[kind]()
    best = None
    for dn, dp in sites.items():
        for an, ap in sites.items():
            if an == dn and kind != "space": continue
            cost = _km(here, dp) + _km(ap, target)
            if best is None or cost < best[0]: best = (cost, dn, dp, an, ap)
    return best[1:]

def special_leg(pk, kind, here, target):
    """Returns (depart_site_name, depart_pt, leg, arrive_site_name, arrive_pt)."""
    v = pk["v"]
    if kind == "water":
        dn, dp, an, ap = site_pair(kind, here, target)
        shape = water_path(dp, ap)
        if not shape: return None
        mi = shape_len(shape) / 1609; sp = speed_mph(kind, v)
        under = re.search(r"sub|submersible|underwater", v.lower())
        steps = [f"Launch your {v.lower()} at {dn}.",
                 f"{'Dive and cruise underwater' if under else 'Head out onto the water'} across the bay, about {mi:.1f} mi on open water.",
                 f"Land at {an}."]
        leg = {"name": v, "mode": "water", "label": f"on the water · {dn} to {an}", "shape": shape, "miles": round(mi, 2),
               "minutes": max(2, round(mi / sp * 60)), "steps": steps, "estimate": True}
        return dn, dp, leg, an, ap
    if kind == "plane":
        dn, dp, an, ap = site_pair(kind, here, target)
        mi = _km(dp, ap) / 1.609; sp = speed_mph(kind, v); big = sp > 150
        steps = [f"Board your {v.lower()} at {dn}" + (" (allow about 45 minutes to check in and board)." if big else "."),
                 f"Take off and fly about {mi:.0f} mi.", f"Land at {an}."]
        leg = {"name": v, "mode": "air", "label": f"in the air · {dn} to {an}", "shape": _arc(dp, ap), "miles": round(mi, 1),
               "minutes": round(mi / sp * 60) + (60 if big else 25), "steps": steps, "dashed": True, "estimate": True}
        return dn, dp, leg, an, ap
    if kind == "fly":
        mi = _km(here, target) / 1.609; sp = speed_mph(kind, v)
        steps = [f"Take off in your {v.lower()} from your start.", f"Fly about {mi:.1f} mi straight toward your polling place.",
                 "Touch down near the polling place."]
        leg = {"name": v, "mode": "air", "label": "in the air", "shape": _arc(here, target, 0.12), "miles": round(mi, 1),
               "minutes": max(2, round(mi / sp * 60) + 10), "steps": steps, "dashed": True, "estimate": True}
        return "your start", here, leg, "your polling place", target
    if kind == "space":
        dn, dp, an, ap = site_pair(kind, here, target)
        ground = _km(dp, ap) / 1.609
        steps = [f"Lift off in your {v.lower()} from {dn}.",
                 "Climb straight up past the Karman line, 100 km (62 mi) above Earth. You're in space.",
                 "Coast for about 4 minutes of weightlessness and look down at the Bay.",
                 "Re-enter the atmosphere over the Pacific" + (f" and land back at {an}." if an == dn else f" and land at {an}.")]
        leg = {"name": v, "mode": "space", "label": f"to space (100 km up) and back · {dn} to {an}", "shape": _space_shape(dp, ap),
               "miles": round(124 + ground), "minutes": 15, "steps": steps, "dashed": True, "estimate": True}
        return dn, dp, leg, an, ap
    return None

def special_route(start, end, picks, polys, leg, chain, when):
    specials = [(pk, special_kind(pk)) for pk in picks]; specials = [(pk, k) for pk, k in specials if k]
    ground = sorted([pk for pk in picks if pk["m"] != "transit" and not special_kind(pk)], key=lambda pk: -RANK[pk["m"]])
    transit = [pk for pk in picks if pk["m"] == "transit"]
    legs, notes, here = [], [], start
    def connect(a, b, to_name, use):
        if _km(a, b) < 0.05: return []
        if use:
            r = chain(use, a, b)
            if r: return r
        pk = {"v": "Walk", "m": "walking"} if _km(a, b) < 1.6 else {"v": "Drive", "m": "driving"}
        r = leg(pk, a, b)
        if r: r["label"] = f"to {to_name}"; return [r]
        return [{"name": pk["v"], "mode": pk["m"], "label": f"to {to_name}", "shape": [list(a), list(b)], "miles": round(_km(a, b) / 1.609, 2),
                 "minutes": max(1, round(_km(a, b) / (5 if pk["m"] == "walking" else 40) * 60)), "steps": [f"Head to {to_name}."], "estimate": True}]
    import itertools
    def ground_km(order):
        h, tot = start, 0.0
        for pk, kind in order:
            dn, dp, an, ap = site_pair(kind, h, end); tot += _km(h, dp); h = ap
        return tot + _km(h, end)
    if 1 < len(specials) <= 6:
        specials = list(min(itertools.permutations(specials), key=ground_km))
    # every ground pick gets used: with transit they all ride to the first launch site,
    # otherwise they're split between the first and last stretch.
    if transit or len(ground) < 2: head, tail = ground, []
    else: head, tail = ground[:-1], ground[-1:]
    for i, (pk, kind) in enumerate(specials):
        sl = special_leg(pk, kind, here, end)
        if not sl: notes.append(f"We couldn't find a way to use the {pk['v'].lower()} on this trip."); continue
        dn, dp, lg, an, ap = sl
        legs += connect(here, dp, dn, head if i == 0 else [])
        legs.append(lg); here = ap
    if transit:
        tr = transit_route(here, end, transit, when)
        if tr: legs += tr["legs"]; notes += tr["notes"]
        else: legs += connect(here, end, "your polling place", tail); notes.append("No transit fits the last stretch, so it's shown another way.")
    else:
        legs += connect(here, end, "your polling place", tail)
    return legs, notes

SPEED_MPH = {"walking": 3, "bicycling": 10, "driving": 25, "transit": 15, "water": 5, "air": 120, "space": 60}
def _interp(a, b, n=24):
    return [[a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n] for i in range(n + 1)]

def straight_leg(pk, a, b):
    """Used only when the routing service can't answer: a direct path, clearly marked approximate."""
    mi = _km(a, b) / 1.609 * 1.25
    return {"name": pk["v"], "mode": pk["m"], "label": "approximate path", "shape": _interp(a, b), "miles": round(mi, 2),
            "minutes": max(1, round(mi / SPEED_MPH.get(pk["m"], 5) * 60)), "steps": [f"Head toward your next stop by {pk['v'].lower()} (about {mi:.1f} mi)."],
            "estimate": True, "approx": True}

def route(body):
    try:
        return _route_flagged(body)
    except Exception:
        start = _start_of(body)
        if not start: return {"ok": False, "error": "start_not_found"}
        end = (float(body["toLat"]), float(body["toLon"]))
        picks = body.get("picks") or [{"v": "Walk", "m": "walking"}]
        pts = [start] + [tuple(point_at(_interp(start, end), shape_len(_interp(start, end)) * (i + 1) / len(picks))) for i in range(len(picks) - 1)] + [end]
        legs = [straight_leg(pk, pts[i], pts[i + 1]) for i, pk in enumerate(picks)]
        return {"ok": True, "start": start, "end": end, "legs": legs, "avoided": [], "skipped": [], "added": [],
                "notes": ["The routing service had trouble, so this is an approximate path."],
                "miles": round(sum(l["miles"] for l in legs), 1), "minutes": sum(l["minutes"] for l in legs)}

def _route_flagged(body):
    """Route, then flag any leg that uses a way of getting around the user didn't pick."""
    out = _route(body)
    if not out.get("ok") or not out.get("legs"): return out
    picks = body.get("picks") or [{"v": body.get("vehicle") or "Walk", "m": body.get("mode", "walking")}]
    names = {pk["v"].lower() for pk in picks}
    any_transit = any(pk["m"] == "transit" for pk in picks)
    walkish = any(pk["m"] == "walking" and not special_kind(pk) for pk in picks)
    added = []
    for l in out["legs"]:
        n = l["name"].lower()
        ok = (n in names or (l["mode"] == "transit" and any_transit)
              or (n in ("walk", "wheelchair", "stroller") and (walkish or n in names)))
        if not ok:
            l["added"] = True
            if l["name"] not in added: added.append(l["name"])
    out["added"] = added
    if any(l.get("approx") for l in out["legs"]):
        out.setdefault("notes", []).append("Some stretches are approximate because the routing service didn't respond.")
    return out

def _start_of(body):
    try: start = geocode(body.get("from", ""))
    except Exception: start = None
    if not start and body.get("fromLat") is not None:
        start = (float(body["fromLat"]), float(body["fromLon"]))
    return start

def _route(body):
    start = _start_of(body)
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
        r = None
        if costing:
            try: r = valhalla(costing, opts, a, b, polys)
            except Exception: r = None
            if not r and polys:
                try: r = valhalla(costing, opts, a, b, [])
                except Exception: r = None
                if r: r["avoidSkipped"] = True
        if not r: r = straight_leg(pk, a, b)
        r.update({"name": pk["v"], "mode": pk["m"], "label": r.get("label") or label or f"{pk['v'].lower()} route"})
        return r
    def chain(seq, a, b):
        """Route a->b using every pick in seq, each over its own stretch (fastest first). Never fails."""
        if len(seq) == 1: return [leg(seq[0], a, b)]
        base = leg(seq[0], a, b)
        shape = base["shape"] if len(base["shape"]) > 2 else _interp(a, b, 24)
        total = shape_len(shape); s = sum(W.get(pk["m"], 1) for pk in seq); acc = 0; cuts = []
        for pk in seq[:-1]:
            acc += W.get(pk["m"], 1); cuts.append(point_at(shape, total * acc / s))
        pts = [a] + cuts + [b]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            return list(ex.map(lambda i: leg(seq[i], pts[i], pts[i + 1]), range(len(seq))))
    notes = []
    if any(special_kind(pk) for pk in picks):
        legs, notes = special_route(start, end, picks, polys, leg, chain, body.get("time", "midday"))
        if not legs: return {"ok": False, "error": "no_route"}
        return {"ok": True, "start": start, "end": end, "legs": legs, "avoided": avoided, "skipped": skipped, "notes": notes,
                "miles": round(sum(l["miles"] for l in legs), 1), "minutes": sum(l["minutes"] for l in legs)}
    if transit_picks:
        tr = transit_route(start, end, picks, body.get("time", "midday"))
        if not tr:
            seq = nt[:1] + transit_picks + nt[1:]
            legs = chain(seq, start, end)
            notes.append("Live transit schedules didn't load, so the transit part is an approximate path. Check the schedule before you go.")
            return {"ok": True, "start": start, "end": end, "legs": legs, "avoided": avoided, "skipped": skipped, "notes": notes,
                    "miles": round(sum(l["miles"] for l in legs), 1), "minutes": sum(l["minutes"] for l in legs)}
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
