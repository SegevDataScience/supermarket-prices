"""Make sure every store's cached price TSV comes from the NEWEST PriceFull file the chain has
published, and record that file's real date.

usage: python fresh_prices.py chain:store_id chain:store_id ...

Why: prices.py (vendor) and extra_chains.py (ext) cache one TSV per *download day*, so a file
downloaded at 00:07 (still yesterday's prices) was reused all day even after the chain published
a newer one at 12:00, and the app showed it as today's. This helper reuses their own listing /
download / parsing code unchanged (same hosts, HTTPS with normal certificate checks, same
safe-name cache writes) and only adds: pick the newest file, compare it with the one cached,
re-download when it differs.

Writes only inside the cache folder, next to the TSV (same names as the two scripts use):
  <chain>-<id>-<TODAY>.tsv    prices (8 columns, as before)
  <chain>-<id>-<TODAY>.date   YYYYMMDD of the chain's file (what the app shows)
  <chain>-<id>-<TODAY>.src    "<chain's file name>\t<store check>"; rewritten on every check
                              (mtime = checked at). The file's own <StoreId> must match the store.
Output lines: "ok   chain:id  ..." / "FAIL chain:id  ..." (the format the app already parses).
"""
import re
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "ext"))
sys.path.insert(1, str(HERE / "vendor" / "il-supermarket-compare" / "skills" / "il-supermarket-compare" / "scripts"))
import chains as V  # noqa: E402  vendor: shufersal, carrefour, publishedprices users
import extra_chains as X  # noqa: E402  ext: the extra chains

TODAY = X.TODAY
STAMP = re.compile(r"-(\d{8})-?(\d{4,6})?(?:\.|$)")
PRINT_LOCK = threading.Lock()


def stamp(name):
    """'PriceFull7290058140886-001-007-20261121-120031.gz' -> '20261121120031' (sortable)."""
    m = STAMP.search(name.rsplit("/", 1)[-1].split("?")[0])
    return (m.group(1) + (m.group(2) or "").ljust(6, "0")) if m else ""


def newest(names):
    names = [n for n in names if stamp(n)]
    if not names:
        raise RuntimeError("no PriceFull file published (closed / Shabbat?)")
    return max(names, key=stamp)


def plugin_source(chain, sid):
    """(file name, download()) for a vendor chain, using the vendor's own listing code."""
    if chain == "shufersal":
        url = newest([u for u in V._shuf_links(2, sid) if "PriceFull" in u])
        return url.rsplit("/", 1)[-1].split("?")[0], lambda: V.requests.get(url, headers=V.UA, timeout=300).content
    if chain == "carrefour":
        path, names = V._car_index()
        f = newest([n for n in names if n.startswith("PriceFull") and f"-{int(sid):03d}-" in n])
        return f, lambda: V.requests.get(f"{V.CAR}/{path}/{f}", headers=V.UA, timeout=300).content
    s = V._pp_session(chain)
    f = newest([n for n in V._pp_ls(s, "PriceFull") if f"-{int(sid):03d}-" in n])
    return f, lambda: s.get(V.PP + "/file/d/" + f, timeout=300).content


def ext_source(chain, sid):
    list_files, download = X.portal(chain)
    f = X.latest(list_files("pricefull", sid), X.PRICEFULL_FILE, sid)
    return f, lambda: download(f)


def refresh(chain, sid):
    base = f"{chain}-{sid}-{TODAY}"
    tsv, side, src = (X.CACHE / f"{base}.{ext}" for ext in ("tsv", "date", "src"))
    try:
        fname, fetch = (ext_source if chain in X.CHAINS else plugin_source)(chain, sid)
    except Exception as e:  # noqa: BLE001 - can't list: keep a cached file if there is one
        if tsv.exists():
            return f"cached  {tsv.name}  NOT VERIFIED ({type(e).__name__}: {str(e)[:120]})"
        raise
    if not X.SAFE_NAME.match(fname):
        raise RuntimeError(f"unsafe file name {fname!r}")
    day = stamp(fname)[:8]
    # .src = "<file name>\t<store check>"; a cached TSV counts only if it came from the same file
    # AND passed the store check (older .src files without the check are downloaded again).
    old = src.read_text(encoding="utf-8").strip().split("\t") if src.exists() else []
    if tsv.exists() and len(old) == 2 and old[0] == fname:
        X.write_cache(src.name, "\t".join(old))  # refresh the "checked at" time
        return f"cached  {tsv.name}  newest file {fname}  file date {day}  {old[1]}"
    content = fetch()
    root = X.parse_xml(content) if chain in X.CHAINS else V.parse_xml(content)
    # The prices must belong to THIS branch: the file's own <StoreId> has to match the one asked for
    # (the file name is only a listing label).
    inside = next(((el.text or "").strip() for el in root.iter() if el.tag.lower() == "storeid" and (el.text or "").strip()), "")
    if inside and inside.isdigit() and int(inside) != int(sid):
        raise RuntimeError(f"file {fname} is for store {int(inside)}, not {sid}")
    rows = X.tsv_rows(root)
    if not rows:
        raise RuntimeError("price file had no <Item> records")
    X.write_cache(tsv.name, "\n".join(rows))
    X.write_cache(side.name, day)
    check = f"store id in file {int(inside)}" if inside.isdigit() else "no store id inside the file"
    X.write_cache(src.name, f"{fname}\t{check}")
    return f"{len(rows)} items  {tsv.name}  newest file {fname}  file date {day}  {check}"


def run_chain(chain, sids):
    for sid in sids:  # one chain = one portal session, sequential
        try:
            if not sid.isdigit():
                raise RuntimeError("store id must be a number")
            if chain not in X.CHAINS and chain not in V.all_chain_keys():
                raise RuntimeError("unknown chain")
            line = f"ok   {chain}:{int(sid)}  {refresh(chain, str(int(sid)))}"
        except Exception as e:  # noqa: BLE001 - report and continue
            line = f"FAIL {chain}:{sid}  {type(e).__name__}: {str(e)[:200]}"
        with PRINT_LOCK:
            print(line, flush=True)


def main():
    by_chain = {}
    for t in sys.argv[1:][:40]:
        chain, _, sid = t.partition(":")
        by_chain.setdefault(chain, []).append(sid)
    threads = [threading.Thread(target=run_chain, args=item) for item in by_chain.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
