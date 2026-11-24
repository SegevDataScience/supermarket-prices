"""Price data for the family page's free search, for the areas in family/regions.json.

Writes site/data/:
  places.json        localities in the areas (closed list): [{n: name, c: code, la, lo}]
  streets/<code>.json  the locality's official street names (data.gov.il)
  stores.json        every branch in the areas: name, kind, coordinates, price date
  products.json      [[key, name, weighted]] - real barcodes are shared across chains
  refs.json          the app's reference products (same product as the app for known items)
  s/<i>.json         one branch's prices: [[product index, price], ...]
Run on the PC (all chains answer from Israel): python family/build_regions.py
"""
import json
import re
import time
import shutil
import os
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("IL_SUPERMARKET_PLUGIN_DIR", str(ROOT / "vendor" / "il-supermarket-compare"))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ["SUPERMARKET_OFFLINE"] = "1"
sys.path.insert(0, str(ROOT))
import app  # noqa: E402

sys.path.insert(1, str(app.SCRIPTS_DIR))
import chains as V  # noqa: E402  vendor
import find_stores as FS  # noqa: E402  vendor (geocode cache, locate_store, is_supermarket)

LIVE = ROOT / "site" / "data"  # what the local app and the published page read
OUT = ROOT / "site" / "data.build"  # built here, then swapped in at once: never half-built data on view
WORK = app.WORK_DIR
DATAGOV = "https://data.gov.il/api/3/action/datastore_search"
STREETS_RES = "9ad3862c-8391-4b2f-84a4-2d4c68625f4b"
PLACES_RES = "5c78e9fa-c2e2-4771-93ff-7f400a12f7ba"
SLACK_KM = 3.0  # stores this far beyond an area still count (a street at the edge with a wider search)


def log(*a):
    print(*a, flush=True)


def datagov(resource):
    rows, offset = [], 0
    while True:
        r = requests.get(DATAGOV, params={"resource_id": resource, "limit": 32000, "offset": offset}, timeout=120).json()
        part = r["result"]["records"]
        rows += part
        offset += len(part)
        if not part or offset >= r["result"]["total"]:
            return rows


def cached(name, fn, days=30):
    path = WORK / f"regions-{name}.json"
    if path.exists() and (datetime.now().timestamp() - path.stat().st_mtime) < days * 86400:
        return json.loads(path.read_text(encoding="utf-8"))
    data = fn()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def main():
    cfg = json.loads((ROOT / "family" / "regions.json").read_text(encoding="utf-8"))
    regions = cfg["regions"]
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "s").mkdir(exist_ok=True)
    (OUT / "streets").mkdir(exist_ok=True)

    # ---- area centres ----
    for rg in regions:
        rg["xy"] = app.settlement_centre(rg["center"])
        if not rg["xy"]:
            raise SystemExit(f"cannot locate area centre {rg['center']}")
    national = bool(cfg.get("national"))  # every locality and every branch in the country
    near = (lambda xy, extra=0.0: bool(xy)) if national else         (lambda xy, extra=0.0: xy and any(app._km(xy, rg["xy"]) <= rg["radius"] + extra for rg in regions))

    # ---- localities (closed list): those of the areas' sub-districts, located once, within an area ----
    places_all = cached("places", lambda: datagov(PLACES_RES), days=60)
    name_of = lambda p: p["שם_ישוב"].strip()
    centres = {name_of(p): p for p in places_all}
    nafot = {p["סמל_נפה"] for p in places_all} if national else         {centres[rg["center"]]["סמל_נפה"] for rg in regions if rg["center"] in centres}
    # OpenStreetMap refusing us = localities and branches silently missing: don't build on that
    if not app.geocode_service_ok():
        raise SystemExit("the map service (OpenStreetMap) refuses requests right now - keeping the current data")
    places = []
    cand = [p for p in places_all if p["סמל_נפה"] in nafot]
    log(f"localities to check: {len(cand)} (sub-districts {sorted(nafot)})")
    unplaced = []
    for p in cand:
        xy = place_centre(p)
        if not xy:
            unplaced.append(name_of(p))
        elif near(xy):
            places.append({"n": name_of(p), "c": int(p["סמל_ישוב"]), "la": round(xy[0], 5), "lo": round(xy[1], 5)})
    # areas (no streets): "הרי ירושלים (אזור)" etc. - searched from the area's centre
    for area in cfg.get("areas", []):
        xy = app.settlement_centre(area["center"])
        if xy:
            places.append({"n": area["name"], "c": 0, "la": round(xy[0], 5), "lo": round(xy[1], 5)})
    log(f"localities not placed reliably (not in the list until the next run): {len(unplaced)} {unplaced[:30]}")
    places.sort(key=lambda x: x["n"])
    log(f"localities in the areas: {len(places)}")

    # ---- streets of those localities ----
    streets = cached("streets", lambda: datagov(STREETS_RES), days=30)
    by_code = {}
    for s in streets:
        by_code.setdefault(int(s["סמל_ישוב"]), set()).add(str(s["שם_רחוב"]).strip())
    for pl in places:
        names = sorted(n for n in by_code.get(pl["c"], ()) if n and n != pl["n"])
        (OUT / "streets" / f'{pl["c"]}.json').write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    (OUT / "places.json").write_text(json.dumps(places, ensure_ascii=False), encoding="utf-8")

    # ---- branches in the areas ----
    job = str(uuid.uuid4())
    app.JOBS[job] = {"id": job, "status": "running", "created_at": app.time.time(), "last_activity_at": app.time.time()}
    env = os.environ.copy()
    extra = app.refresh_extra_store_lists(job, env)
    codes = FS.city_names()
    today = datetime.now().strftime("%Y%m%d")
    rows = []
    for chain in V.all_chain_keys():
        try:
            rows += V.cached_json(f"stores-{chain}", lambda c=chain: V.stores_for(c))
        except Exception as exc:  # noqa: BLE001
            log(f"skip {chain}: {exc}")
    for key in extra:
        f = app.CACHE_DIR / f"stores-{key}-{today}.json"
        if f.exists():
            rows += json.loads(f.read_text(encoding="utf-8"))
    stores = []
    nulls_before = app.vendor_geo_nulls()
    for s in rows:
        c = str(s.get("city") or "").strip()
        s["city"] = codes.get(c.lstrip("0"), c) if c.isdigit() else c
        if not s["city"] or s["city"] == "0" or not FS.is_supermarket(s) \
                or app.NOT_SUPERMARKET_RE.search(f'{s.get("name", "")} {s.get("address", "")}'):
            continue
        town = app.settlement_centre(s["city"])
        if not near(town, SLACK_KM + 6):
            continue
        xy, approx = FS.locate_store(s), False
        if xy and town and app._km(xy, town) > app.MAX_FROM_TOWN_KM:
            xy = None  # a same-named street in another town
        if not xy:
            g, level = app.locate_store_better({**s, "chain_name": app.CHAIN_NAMES.get(s["chain"], s.get("chain_name"))})
            xy, approx = g, level == "city"
        if near(xy, SLACK_KM):
            stores.append({**s, "xy": xy, "approx": approx})
    log(f"branches in the areas: {len(stores)}")
    purged = app.forget_failed_vendor_lookups(nulls_before)
    if purged or app.GEO_STATE["failures"] > 20:
        raise SystemExit(f"the map service refused {app.GEO_STATE['failures']} lookups ({purged} store lookups "
                         "to retry) - keeping the current data; the next run continues from the cache")

    # ---- newest prices for all of them (fresh_prices.py, 40 per call) ----
    refs = [f'{s["chain"]}:{s["store_id"]}' for s in stores] + [f'{d["chain"]}:{d["store_id"]}' for d in app.DELIVERY_STORES]
    ok = set()
    for i in range(0, len(refs), 40):
        part = refs[i:i + 40]
        out = subprocess.run([sys.executable, "-B", str(app.FRESH_SCRIPT), *part], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=3600, env=env).stdout
        ok |= {f"{c}:{n}" for c, n in app.OK_LINE.findall(out)}
        log(f"prices {min(i + 40, len(refs))}/{len(refs)}: ok {len(ok)}")

    # ---- products + per-branch files ----
    for old in (OUT / "s").glob("*.json"):
        old.unlink()
    fresh = {}
    products, index, names, meta = [], {}, [], []
    entries = [(s, False) for s in stores] + [({**d, "chain_name": app.CHAIN_NAMES.get(d["chain"])}, True) for d in app.DELIVERY_STORES]
    for s, delivery in entries:
        ref = f'{s["chain"]}:{s["store_id"]}'
        tsv = app.CACHE_DIR / f'{s["chain"]}-{s["store_id"]}-{today}.tsv'
        if ref not in ok or not tsv.exists():
            continue
        data = []
        for r in app._load_tsv(tsv):
            code = r[0].strip()
            key = code if code.isdigit() and len(code) >= 8 else f'{s["chain"]}:{code}'
            if key not in index:
                index[key] = len(products)
                products.append([key, r[1].strip(), 1 if r[5] == "1" else 0])
                names.append([])
            names[index[key]].append(r[1].strip())
            data.append([index[key], round(r[2], 2)])
        i = len(meta)
        (OUT / "s" / f"{i}.json").write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        side = tsv.with_suffix(".date")
        day = side.read_text(encoding="utf-8").strip() if side.exists() else today
        label = app.tidy_store_name(f'{s.get("chain_name") or app.CHAIN_NAMES.get(s["chain"], s["chain"])} {s.get("name", "")}')
        meta.append({"name": label, "ref": ref,
                     "kind": "delivery" if delivery else ("convenience" if s["chain"] in app.CONVENIENCE_CHAINS else "store"),
                     "addr": ", ".join(x for x in (s.get("address"), s.get("city")) if x),
                     "la": round(s["xy"][0], 5) if not delivery else None, "lo": round(s["xy"][1], 5) if not delivery else None,
                     "approx": bool(s.get("approx")), "date": f"{day[:4]}-{day[4:6]}-{day[6:8]}"})
    for i, ns in enumerate(names):  # one display name: the first word most chains agree on
        heads = Counter(n.split()[0] for n in ns if n.split())
        if heads:
            top = heads.most_common(1)[0][0]
            products[i][1] = max((n for n in ns if n.split() and n.split()[0] == top), key=len)
    known = {}
    for k, v in app._read_json(WORK / "ref_products.json", {}).items():
        name = k.split("|")[0].strip()
        if isinstance(v, dict) and v.get("code") and v["code"] in index and name not in known:
            known[name] = index[v["code"]]
    (OUT / "products.json").write_text(json.dumps(products, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (OUT / "refs.json").write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
    (OUT / "stores.json").write_text(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), "stores": meta},
                                                ensure_ascii=False), encoding="utf-8")
    n, ex = clean_price_files(OUT)
    log(f"dropped {n} impossible prices, e.g. {ex[:4]}")
    moved = fix_far_stores(OUT)
    log(f"moved to their own town: {len(moved)} {moved[:5]}")
    swap_in()
    log(f"done: {len(meta)} branches with prices, {len(products)} products, {len(places)} localities")


def clean_price_files(folder):
    """The chains' own files have placeholder and typo prices (an orange at 0.01, a tomato at 10,000).
    One of those makes a store look cheapest. Drop: under 0.10 or 5,000 and up; for a barcode sold in
    4+ branches, a price 5x away from its median; and keep one row per product per branch (the lower).
    Idempotent. Returns (rows dropped, examples)."""
    files = sorted((folder / "s").glob("*.json"), key=lambda f: int(f.stem))
    data = {f: json.loads(f.read_text(encoding="utf-8")) for f in files}
    by_code = {}
    for rows in data.values():
        for pi, price in rows:
            by_code.setdefault(pi, []).append(price)
    median = {pi: sorted(v)[len(v) // 2] for pi, v in by_code.items() if len(v) >= 4}
    dropped, examples = 0, []
    for f, rows in data.items():
        best = {}
        for pi, price in rows:
            m = median.get(pi)
            bad = price < 0.10 or price >= 5000 or (m and abs(price - m) > 3 and (price > 5 * m or price < m / 5))
            if bad:
                dropped += 1
                if len(examples) < 8:
                    examples.append((f.stem, pi, price, m))
            elif pi not in best or price < best[pi]:
                best[pi] = price
        clean = [[pi, p] for pi, p in best.items()]
        if len(clean) != len(rows):
            f.write_text(json.dumps(clean, separators=(",", ":")), encoding="utf-8")
    return dropped, examples


JS_NAFOT = range(71, 78)  # Judea and Samaria sub-districts


def west_of_green_line(lo):
    return lo < 34.93  # no Judea-and-Samaria locality lies this far west (Alfei Menashe/Oranit are ~34.98)


def district_ok(display, nafa_name):
    """The map's answer names its district ("..., נפת תל אביב, מחוז תל אביב, ישראל"): it must not be another
    district than the one data.gov gives for that locality (אפרת: data.gov בית לחם, map hit נפת תל אביב)."""
    named = [app._loose(n) for n in re.findall(r"נפת ([^,]+)", display or "")]
    want = app._loose(nafa_name)
    return not named or not want or any(n[:3] == want[:3] for n in named)


def place_centre(p):
    """A locality's centre from a settlement lookup that is checked against its official district; on a
    mismatch, once more by its English name; None when it can't be placed reliably."""
    name, nafa = " ".join(str(p["שם_ישוב"]).split()), str(p.get("שם_נפה") or "").strip()
    english = " ".join(str(p.get("שם_ישוב_לועזי") or "").split()).title()
    js = p["סמל_נפה"] in JS_NAFOT
    for q in [name] + ([english] if english else []):
        if js:  # OpenStreetMap files Judea and Samaria under "ps": with "il" only, a same-named street in Israel comes back
            hit = app._nominatim(f"settlement-ilps:{q}", {"q": q, "featureType": "settlement", "countrycodes": "il,ps"}, with_name=True)
        else:
            hit = app._nominatim(f"settlement:{q}", {"q": q, "featureType": "settlement"}, with_name=True)
        if hit and district_ok(hit[2], nafa) and not (p["סמל_נפה"] in JS_NAFOT and west_of_green_line(hit[1])):
            return hit[:2]
    return None


def misplaced_places(places, raw_by_code):
    """Certain errors only: a Judea-and-Samaria locality placed west of the Green Line."""
    return {pl["n"] for pl in places if pl.get("c") and pl["c"] in raw_by_code
            and raw_by_code[pl["c"]]["סמל_נפה"] in JS_NAFOT and west_of_green_line(pl["lo"])}


def hide_suspicious_places(folder):
    """No map service needed (--clean): hide misplaced localities and every branch placed through them
    (its town-centre position, or a street hit in the wrong town). The next full build looks them up again."""
    raw = {int(r["סמל_ישוב"]): r for r in json.loads((WORK / "regions-places.json").read_text(encoding="utf-8"))}
    places = json.loads((folder / "places.json").read_text(encoding="utf-8"))
    bad = misplaced_places(places, raw)
    if not bad:
        return []
    (folder / "places.json").write_text(json.dumps([pl for pl in places if pl["n"] not in bad], ensure_ascii=False), encoding="utf-8")
    doc = json.loads((folder / "stores.json").read_text(encoding="utf-8"))
    loose_bad = {app._loose(n) for n in bad}
    for st in doc["stores"]:
        town = st.get("addr", "").rsplit(",", 1)[-1].strip()
        if st.get("la") is not None and app._loose(town) in loose_bad and west_of_green_line(st["lo"]):
            st["la"] = st["lo"] = None  # not shown in any radius (indices of data/s/<i>.json stay)
    (folder / "stores.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return sorted(bad)


def fix_far_stores(folder):
    """A branch must be in (or next to) its own town. A town lookup sometimes lands on a same-named street
    elsewhere ("מחסני השוק אפרת" showed up next to Dizengoff, Tel Aviv: there is an אפרת street). With the
    locality list (one settlement lookup per locality), a branch more than MAX_FROM_TOWN_KM from its town's
    centre is put at that centre and marked approximate. Idempotent. Returns the branches moved."""
    doc = json.loads((folder / "stores.json").read_text(encoding="utf-8"))
    places = json.loads((folder / "places.json").read_text(encoding="utf-8"))
    by_name = {}
    for p in places:
        if p.get("c"):
            by_name.setdefault(app._loose(p["n"]), p)
    def town_of(addr):
        t = app._loose(addr.rsplit(",", 1)[-1]) if addr else ""
        if not t:
            return None
        if t in by_name:
            return by_name[t]
        near = [p for n, p in by_name.items() if len(t) >= 4 and len(n) >= 4 and (n.startswith(t) or t.startswith(n))]
        return near[0] if len(near) == 1 else None  # "תל אביב" -> "תל אביב - יפו"; ambiguous: leave it
    moved = []
    for s in doc["stores"]:
        if s.get("la") is None:
            continue
        p = town_of(s.get("addr", ""))
        if p and app._km((s["la"], s["lo"]), (p["la"], p["lo"])) > app.MAX_FROM_TOWN_KM:
            moved.append(f'{s["name"]} ({s["addr"]})')
            s["la"], s["lo"], s["approx"] = p["la"], p["lo"], True
    if moved:
        (folder / "stores.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return moved


def swap_in():
    """Replace site/data with the finished build (retries: a file being served can block a rename briefly)."""
    old = LIVE.with_name("data.old")
    shutil.rmtree(old, ignore_errors=True)
    for attempt in range(30):
        try:
            if LIVE.exists():
                LIVE.rename(old)
            OUT.rename(LIVE)
            break
        except OSError:
            if old.exists() and not LIVE.exists():
                old.rename(LIVE)  # put the previous data back before retrying
            time.sleep(2)
    else:
        raise SystemExit("could not swap the new data in (site/data is in use)")
    shutil.rmtree(old, ignore_errors=True)


if __name__ == "__main__":
    if "--clean" in sys.argv:  # only clean the current site/data
        print(clean_price_files(LIVE))
        print("hidden localities:", hide_suspicious_places(LIVE))
        print("moved branches:", fix_far_stores(LIVE))
    else:
        main()
