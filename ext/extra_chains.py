"""Store lists and full price files for chains the il-supermarket-compare plugin doesn't cover.

Writes the plugin's own cache formats into IL_SUPERMARKETS_CACHE (default
~/.cache/il-supermarkets), so the plugin's find_stores.py / search.py / basket.py use
them unchanged:
  stores-<chain>-YYYYMMDD.json   list of {chain, chain_name, store_id, name, address, city, subchain}
  <chain>-<store_id>-YYYYMMDD.tsv the plugin's 8 TSV columns (see COLS)
  <chain>-<store_id>-YYYYMMDD.date the chain's own file date (may be yesterday's file)

usage:
  python extra_chains.py stores                    # today's store list of every extra chain
  python extra_chains.py prices victory:12 yellow:100 ...
  python extra_chains.py prices --nearby [K]       # K nearest branches of each extra chain
                                                   # in stores_nearby.json (default 2)

Endpoints and file layouts were looked up in the il-supermarket-scraper project
(github.com/OpenIsraeliSupermarkets/israeli-supermarket-scarpers, non-commercial
licence); this file is an independent minimal implementation. HTTPS only, and only
the hosts in ALLOWED_HOSTS. Standard library + requests; no subprocess, no eval/exec.
"""
import argparse, datetime, gzip, io, json, os, re, sys, zipfile
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests

CACHE = Path(os.environ.get("IL_SUPERMARKETS_CACHE", Path.home() / ".cache" / "il-supermarkets"))
TODAY = datetime.date.today().strftime("%Y%m%d")
OLDEST = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
UA = {"User-Agent": "il-supermarket-compare/1.0 (personal price comparison)"}
TIMEOUT = 120
MAX_BYTES = 80 * 1024 * 1024  # a full price file is normally 0.1-10 MB
MAX_UNPACKED = 300 * 1024 * 1024  # cap on the decompressed XML
COLS = ["itemcode", "itemname", "itemprice", "quantity", "unitofmeasure", "bisweighted",
        "unitofmeasureprice", "manufacturername"]  # same order as the plugin's prices.py

PP = "https://url.publishedprices.co.il"
LAIB = "https://laibcatalog.co.il"
HAZI = "https://shop.hazi-hinam.co.il/Prices"
# Public access credentials the chains publish under the price-transparency regulations.
CHAINS = {
    "victory":      {"name": "ויקטורי", "portal": "laib", "edi": "7290696200003"},
    "mahsaniashuk": {"name": "מחסני השוק", "portal": "laib", "edi": "7290661400001"},
    "hazihinam":    {"name": "חצי חינם", "portal": "hazi"},
    "superyuda":    {"name": "סופר יודה", "portal": "pp", "user": "yuda_ho", "password": "Yud@147", "folder": "Yuda"},
    "yellow":       {"name": "יילו", "portal": "pp", "user": "Paz_bo", "password": "paz468", "folder": ""},
    "shukhair":     {"name": "שוק העיר", "portal": "bina", "host": "shuk-hayir.binaprojects.com", "chain_id": "7290058148776"},
    "kingstore":    {"name": "קינג סטור", "portal": "bina", "host": "kingstore.binaprojects.com", "chain_id": "7290058108879"},
    "supersapir":   {"name": "סופר ספיר", "portal": "bina", "host": "supersapir.binaprojects.com", "chain_id": "7290058156016"},
    "zolvebegadol": {"name": "זול ובגדול", "portal": "bina", "host": "zolvebegadol.binaprojects.com", "chain_id": "7290058173198"},
}
ALLOWED_HOSTS = {
    "url.publishedprices.co.il", "laibcatalog.co.il", "shop.hazi-hinam.co.il",
    "hazihinamprod01.blob.core.windows.net",
    "shuk-hayir.binaprojects.com", "kingstore.binaprojects.com",
    "supersapir.binaprojects.com", "zolvebegadol.binaprojects.com",
}
# Branches whose stores file gives no city ("0"); find_stores skips a store without one.
CITY_FIX = {("supersapir", "52"): "קרית יערים"}  # נטו חסכון טלזסטון
SKIP_STORE = re.compile(r"וולט|wolt", re.I)  # Wolt dark stores listed in some chains' files
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
STORES_FILE = re.compile(r"^Stores(?:Full)?\d+-\d+-(\d{8})", re.I)
PRICEFULL_FILE = re.compile(r"^PriceFull\d+-\d+-(\d+)-(\d{8})", re.I)


# ---------------- network (HTTPS + host allowlist + size cap) ----------------
def check_url(url):
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS:
        raise RuntimeError(f"refusing to contact {u.scheme}://{u.hostname}")
    return url


def fetch(session, method, url, hops=0, **kw):
    r = session.request(method, check_url(url), timeout=TIMEOUT, stream=True, allow_redirects=False, **kw)
    if r.is_redirect:  # follow redirects ourselves so every hop is checked
        if hops >= 5:
            raise RuntimeError("too many redirects")
        return fetch(session, "GET", urljoin(url, r.headers["Location"]), hops + 1)
    r.raise_for_status()
    data = bytearray()
    for chunk in r.iter_content(64 * 1024):
        data += chunk
        if len(data) > MAX_BYTES:
            raise RuntimeError(f"response from {urlparse(url).hostname} is larger than {MAX_BYTES} bytes")
    return bytes(data)


def new_session():
    s = requests.Session()
    s.headers.update(UA)
    return s


# ---------------- XML -> plugin formats ----------------
def read_capped(f):
    """Read a decompressing stream in chunks; stop past MAX_UNPACKED (zip/gzip bombs)."""
    data = bytearray()
    while chunk := f.read(1024 * 1024):
        data += chunk
        if len(data) > MAX_UNPACKED:
            raise RuntimeError(f"file unpacks to more than {MAX_UNPACKED} bytes")
    return bytes(data)


def parse_xml(content):
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            members = z.infolist()
            if len(members) != 1:
                raise RuntimeError(f"zip has {len(members)} members, expected 1")
            if members[0].file_size > MAX_UNPACKED:
                raise RuntimeError(f"zip member is {members[0].file_size} bytes, over the cap")
            with z.open(members[0]) as f:
                content = read_capped(f)
    elif content[:2] == b"\x1f\x8b":
        with gzip.GzipFile(fileobj=io.BytesIO(content)) as f:
            content = read_capped(f)
    return ET.fromstring(content)


def records(root, tag):
    for el in root.iter():
        if el.tag.lower() == tag and len(el):
            yield {c.tag.lower(): (c.text or "").strip() for c in el}


def store_rows(root, key):
    rows = []
    for d in records(root, "store"):
        sid = d.get("storeid", "").lstrip("0") or "0"
        city = d.get("city", "")
        if city.strip("0 ") == "":
            city = CITY_FIX.get((key, sid), city)
        if not SKIP_STORE.search(d.get("storename", "")):
            rows.append({"chain": key, "chain_name": CHAINS[key]["name"], "store_id": sid,
                         "name": d.get("storename", ""), "address": d.get("address", ""), "city": city,
                         "subchain": d.get("subchainname", "")})
    return rows


def tsv_rows(root):
    rows = []
    for d in records(root, "item"):
        d.setdefault("manufacturername", d.get("manufacturename", ""))
        rows.append("\t".join((d.get(k) or "").replace("\t", " ").replace("\n", " ") for k in COLS))
    return rows


def latest(names, pattern, store_id=None):
    """Newest file name (by date in the name) matching pattern, optionally for one store."""
    best = []
    for n in names:
        m = pattern.match(n)
        if not m or not SAFE_NAME.match(n):
            continue
        groups = m.groups()
        if store_id is not None and int(groups[0]) != int(store_id):
            continue
        if groups[-1] >= OLDEST:
            best.append((groups[-1], n))
    if not best:
        what = f"PriceFull for store {store_id}" if store_id is not None else "Stores file"
        raise RuntimeError(f"no {what} published since {OLDEST} (closed / Shabbat?)")
    return max(best)[1]


# ---------------- portals: each returns (list_files(kind, store_id), download(name)) ----------------
def portal_pp(cfg):
    s = new_session()
    page = fetch(s, "GET", PP + "/login").decode("utf-8", "replace")
    tok = re.search(r'name="csrftoken" content="([^"]+)"', page).group(1)
    fetch(s, "POST", PP + "/login/user", data={"username": cfg["user"], "password": cfg["password"], "csrftoken": tok})
    m = re.search(r'name="csrftoken" content="([^"]+)"', fetch(s, "GET", PP + "/file").decode("utf-8", "replace"))
    if not m:
        raise RuntimeError(f"publishedprices login failed for {cfg['user']}")
    listing = json.loads(fetch(s, "POST", PP + "/file/json/dir", data={
        "sEcho": 1, "iDisplayStart": 0, "iDisplayLength": 100000, "sSearch": "",
        "cd": "/" + cfg["folder"], "csrftoken": m.group(1)}))
    names = [f["fname"] for f in listing["aaData"]]
    prefix = cfg["folder"] + "/" if cfg["folder"] else ""
    return (lambda kind, store_id: names), (lambda name: fetch(s, "GET", f"{PP}/file/d/{prefix}{name}"))


def portal_laib(cfg):
    s = new_session()
    listing = json.loads(fetch(s, "GET", f"{LAIB}/webapi/api/getfiles", params={"edi": cfg["edi"]}))
    names = [f.get("fileName", "") for f in listing]
    return (lambda kind, store_id: names), (lambda name: fetch(s, "GET", f"{LAIB}/webapi/{cfg['edi']}/{name}"))


def portal_bina(cfg):
    s = new_session()
    base = f"https://{cfg['host']}/"

    def list_files(kind, store_id):
        params = {"_": cfg["chain_id"], "wReshet": "הכל", "WFileType": 1 if kind == "stores" else 4,
                  "WDate": "", "WStore": store_id or ""}
        return [f.get("FileNm", "") for f in json.loads(fetch(s, "GET", base + "MainIO_Hok.aspx", params=params))]

    def download(name):
        spath = json.loads(fetch(s, "GET", base + "Download.aspx", params={"FileNm": name}))[0]["SPath"]
        return fetch(s, "GET", spath)  # SPath must also be an allowed https host
    return list_files, download


def portal_hazi(cfg):
    s = new_session()
    links = {}

    def list_files(kind, store_id):
        file_type = "3" if kind == "stores" else "1"
        for day in (datetime.date.today(), datetime.date.today() - datetime.timedelta(days=1)):
            for page in range(1, 11):
                html = fetch(s, "GET", HAZI, params={"d": day.isoformat(), "t": file_type, "f": "null", "p": page})
                found = re.findall(r'href="(https://hazihinamprod01\.blob\.core\.windows\.net/[^"]+)"',
                                   html.decode("utf-8", "replace"))
                new = {u.rsplit("/", 1)[-1]: u for u in found if u.rsplit("/", 1)[-1] not in links}
                if not new:
                    break
                links.update(new)
        return list(links)
    return list_files, (lambda name: fetch(s, "GET", links[name]))


PORTALS = {"pp": portal_pp, "laib": portal_laib, "bina": portal_bina, "hazi": portal_hazi}
_open = {}


def portal(key):
    if key not in _open:
        _open[key] = PORTALS[CHAINS[key]["portal"]](CHAINS[key])
    return _open[key]


# ---------------- cache writes (only inside CACHE) ----------------
def write_cache(filename, text):
    if not SAFE_NAME.match(filename):
        raise RuntimeError(f"unsafe cache file name {filename!r}")
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / filename
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def update_stores(key):
    name = f"stores-{key}-{TODAY}.json"
    if (CACHE / name).exists():
        return f"cached {name}"
    list_files, download = portal(key)
    rows = store_rows(parse_xml(download(latest(list_files("stores", None), STORES_FILE))), key)
    if not rows:
        raise RuntimeError("stores file had no <Store> records")
    write_cache(name, json.dumps(rows, ensure_ascii=False))
    return f"{len(rows)} stores  {name}"


def date_note(file_date):
    return f"file date {file_date}" + ("" if file_date == TODAY else " (OLDER THAN TODAY)")


def update_prices(key, store_id):
    # The TSV keeps the plugin's name (download day = TODAY); the chain's real file date
    # goes in a sidecar <chain>-<id>-<TODAY>.date so the app can show and flag it.
    name = f"{key}-{store_id}-{TODAY}.tsv"
    side = f"{key}-{store_id}-{TODAY}.date"
    if (CACHE / name).exists() and (CACHE / side).exists():
        return f"cached  {name}  {date_note((CACHE / side).read_text(encoding='utf-8').strip())}"
    list_files, download = portal(key)
    fname = latest(list_files("pricefull", store_id), PRICEFULL_FILE, store_id)
    file_date = PRICEFULL_FILE.match(fname).group(2)
    rows = tsv_rows(parse_xml(download(fname)))
    if not rows:
        raise RuntimeError("price file had no <Item> records")
    write_cache(name, "\n".join(rows))
    write_cache(side, file_date)
    return f"{len(rows)} items  {name}  {date_note(file_date)}"


def nearby_targets(per_chain):
    near = json.loads((CACHE / "stores_nearby.json").read_text(encoding="utf-8"))  # sorted by distance
    seen, out = {}, []
    for s in near:
        key = s.get("chain")
        if key in CHAINS and seen.get(key, 0) < per_chain:
            seen[key] = seen.get(key, 0) + 1
            out.append(f"{key}:{s.get('store_id')}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stores")
    st.add_argument("--chains", default=",".join(CHAINS))
    pr = sub.add_parser("prices")
    pr.add_argument("targets", nargs="*", help="chain:store_id")
    pr.add_argument("--nearby", type=int, nargs="?", const=2, default=None)
    a = ap.parse_args()

    failed = 0
    if a.cmd == "stores":
        jobs = [(k, None) for k in a.chains.split(",")]
    else:
        targets = list(a.targets) + (nearby_targets(a.nearby) if a.nearby else [])
        jobs = [tuple(t.split(":", 1)) if ":" in t else (t, "") for t in targets][:30]
    for key, sid in jobs:
        label = key if sid is None else f"{key}:{sid}"
        try:
            if key not in CHAINS:
                raise RuntimeError(f"not an extra chain (known: {', '.join(CHAINS)})")
            if sid is not None and not sid.isdigit():
                raise RuntimeError("store id must be a number")
            info = update_stores(key) if sid is None else update_prices(key, str(int(sid)))
            print(f"ok   {label}  {info}")
        except Exception as e:  # noqa: BLE001 - report and continue with the other chains/stores
            failed += 1
            print(f"FAIL {label}  {type(e).__name__}: {str(e)[:200]}")
    if a.cmd == "prices" and not jobs:
        print("no targets (no extra-chain stores in stores_nearby.json?)")
    return 1 if failed and failed == len(jobs) else 0


if __name__ == "__main__":
    sys.exit(main())
