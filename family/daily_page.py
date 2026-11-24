"""Daily family page: run the app's own search pipeline for the configured lists (no Claude: only
products already decided on the PC are compared) and write a static page + Excel files to site/.

    python family/daily_page.py                 # uses family/config.json (local, not committed)
    FAMILY_CONFIG='{...}' python family/daily_page.py   # the GitHub Actions run: config from a secret

The address is used for the search only; it never appears on the page.
"""
import html
import re
import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["SUPERMARKET_OFFLINE"] = "1"
os.environ.setdefault("IL_SUPERMARKET_PLUGIN_DIR", str(ROOT / "vendor" / "il-supermarket-compare"))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, str(ROOT))
import app  # noqa: E402  the same pipeline as the local app

SITE = ROOT / "site"
DATA = ROOT / "family" / "data"  # product decisions made on the PC (search patterns, reference products)


def load_config():
    raw = os.environ.get("FAMILY_CONFIG")
    if raw:
        return json.loads(raw)
    return json.loads((ROOT / "family" / "config.json").read_text(encoding="utf-8"))


def seed_decisions():
    """Copy the PC's product decisions into the app's work folder (the cloud run starts empty)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return  # on the PC the app's own decisions are the newest
    app.WORK_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("queries_cache.json", "ref_products.json"):
        src = DATA / name
        if src.exists():
            (app.WORK_DIR / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def plan_items(items):
    out = []
    for it in items:
        if "unit" in it:  # list-builder rows: same cleaning as the app
            out += app.clean_items([it])
        else:  # free-text style: name + quantity string exactly as searched on the PC
            out.append({"name": str(it["name"]).strip(), "quantity": (str(it.get("quantity") or "").strip() or None)})
    return out


def run_list(entry):
    plan = {"address": entry["address"], "items": plan_items(entry["items"]), "speed": "fast",
            "location": {"mode": "radius", "radius_km": float(entry.get("radius_km", 4)), "auto_expand": True}}
    job_id = str(uuid.uuid4())
    app.JOBS[job_id] = {"id": job_id, "status": "running", "created_at": app.time.time(), "plan": plan,
                        "last_activity_at": app.time.time()}
    env = os.environ.copy()
    extra = app.refresh_extra_store_lists(job_id, env)
    result, _ = app.fast_search(job_id, plan, env, extra)
    result = app.tidy_result(result)
    job = {"id": job_id, "plan": plan, "result": result, "price_freshness": app.price_freshness(result),
           "computed_at": datetime.now().isoformat(timespec="minutes")}
    return job


def money(v):
    return f"₪{v:,.2f}" if isinstance(v, (int, float)) else "—"


def km(v):
    return f'{v:.1f} ק"מ' if isinstance(v, (int, float)) else "משלוח"


def render(jobs, titles, stamp, notice=""):
    e = html.escape
    parts = []
    for n, (job, title) in enumerate(zip(jobs, titles), start=1):
        r = job["result"]
        w = r.get("winner") or {}
        dates = sorted({f["date"] for f in job["price_freshness"].values() if f.get("date")})
        stores = [s for s in r.get("stores") or [] if s.get("kind") != "convenience"]
        rows = "".join(
            f'<tr{" class=best" if s["store"] == w.get("store") else ""}><td>{e(s["store"])}'
            f'{"<span class=sub>משלוח עד הבית</span>" if s.get("kind") == "delivery" else ""}</td>'
            f'<td class=num>{"≈ " if s.get("approx_location") else ""}{km(s.get("distance_km"))}</td>'
            f'<td class=num>{money(s.get("total_ils"))}<span class=sub>{s.get("items_found")}/{s.get("items_total")} מוצרים'
            f'{" · " + str(s.get("substitutes")) + " תחליפים" if s.get("substitutes") else ""}</span></td></tr>'
            for s in stores)
        item_rows = []
        for it in r.get("items") or []:
            picks = [p for p in it.get("picks") or [] if isinstance(p.get("line_total_ils"), (int, float))
                     and next((s for s in stores if s["store"] == p["store"]), None)]
            best = min(picks, key=lambda p: p["line_total_ils"]) if picks else None
            mine = next((p for p in it.get("picks") or [] if p["store"] == w.get("store")), None)
            item_rows.append(
                f'<tr><td>{e(it["requested"])}</td>'
                f'<td>{e(best["store"]) + " · " + money(best["line_total_ils"]) if best else "לא נמצא"}</td>'
                f'<td>{(e(mine["product"]) + " · " + money(mine["line_total_ils"])) if mine and mine.get("product") else "—"}</td></tr>')
        notes = "".join(f"<li>{e(a)}</li>" for a in r.get("assumptions") or [])
        parts.append(f"""
<section class=card>
  <h2>{e(title)}</h2>
  <div class=meta>מחירים מ-{e(", ".join(datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m.%Y") for d in dates) or "—")}</div>
  <div class=hero><div class=tag>הסל הכי זול</div><div class=store>{e(w.get("store") or "—")}</div>
    <div class=total>{money(w.get("total_ils"))}</div>
    <div class=sub>{w.get("items_found", "")} מתוך {w.get("items_total", "")} מוצרים{(" · " + str(w["substitutes"]) + " תחליפים") if w.get("substitutes") else ""}</div></div>
  <table><thead><tr><th>חנות</th><th>מרחק</th><th>מחיר</th></tr></thead><tbody>{rows}</tbody></table>
  <details><summary>לפי מוצר</summary><table><thead><tr><th>מוצר</th><th>הכי זול</th><th>בחנות הזולה</th></tr></thead>
    <tbody>{"".join(item_rows)}</tbody></table></details>
  <details><summary>הערות</summary><ul>{notes}</ul></details>
  <p class=dl>הורדה לאקסל: <a href="list{n}-compare.xlsx" download>השוואת חנויות</a> · <a href="list{n}-items.xlsx" download>כל המוצרים</a></p>
</section>""")
    return f"""<!doctype html><html lang=he dir=rtl><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content="noindex,nofollow">
<title>איפה הכי זול לקנות</title><style>
:root{{--bg:#0b1020;--card:#141a33;--line:#2a3360;--text:#eef1ff;--muted:#9aa3c7;--accent:#22d3ee;--lime:#a3e635}}
@media (prefers-color-scheme:light){{:root{{--bg:#f5f7fb;--card:#fff;--line:#dde3f0;--text:#141a33;--muted:#5b6485;--accent:#0891b2;--lime:#4d7c0f}}}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,"Segoe UI",Arial,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:16px}} h1{{font-size:1.5rem;margin:8px 0}} h2{{margin:0 0 4px;font-size:1.2rem}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}}
.meta,.sub{{color:var(--muted);font-size:.85rem}} .sub{{display:block}}
.hero{{margin:12px 0}} .tag{{color:var(--accent);font-weight:700;font-size:.9rem}} .store{{font-size:1.3rem;font-weight:800}}
.total{{font-size:2rem;font-weight:800;color:var(--lime)}}
table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:.92rem;table-layout:fixed}}
th,td{{text-align:right;padding:8px 6px;border-top:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}}
th{{color:var(--muted);font-weight:600}} th:first-child{{width:46%}} .num .sub{{font-size:.78rem}} tr.best td{{background:rgb(34 211 238/.10)}}
html,body{{max-width:100%}}
details{{margin-top:10px}} summary{{cursor:pointer;font-weight:600}} ul{{color:var(--muted);font-size:.85rem}} a{{color:var(--accent)}}
.dl{{font-size:.9rem;margin-top:12px}}
.notice{{background:#78350f;color:#fff7ed;border-radius:12px;padding:10px 14px;margin:12px 0;font-size:.92rem}} footer{{color:var(--muted);font-size:.8rem;margin:18px 0}}
</style></head><body><main>
<h1>איפה הכי זול לקנות?</h1>
<div class=meta>עודכן {e(stamp)} · מחירי מדף מהקבצים הרשמיים של הרשתות, בלי מבצעים ומחירי מועדון</div>
{f'<div class=notice>{e(notice)}</div>' if notice else ""}
{"".join(parts)}
<footer>מתעדכן אוטומטית כל בוקר. הסל הכי זול = הסכום הנמוך ביותר לכל המוצרים, מבין החנויות שיש בהן הכי הרבה מהמוצרים (כולל תחליפים).</footer>
</main></body></html>"""


def main():
    cfg = load_config()
    seed_decisions()
    SITE.mkdir(parents=True, exist_ok=True)
    jobs, titles = [], []
    for n, entry in enumerate(cfg["lists"], start=1):
        print(f"== {entry.get('title', n)}", flush=True)
        job = run_list(entry)
        jobs.append(job)
        titles.append(entry.get("title") or f"רשימה {n}")
        w = job["result"].get("winner") or {}
        print(f"   winner: {w.get('store')} {w.get('total_ils')} ({w.get('items_found')}/{w.get('items_total')})", flush=True)
        for kind in ("compare", "items"):
            _, data = app.export_job({**job, "plan": {**job["plan"], "address": "ליד הבית"}}, kind)
            (SITE / f"list{n}-{kind}.xlsx").write_bytes(data)
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    notice = ""
    if os.environ.get("GITHUB_ACTIONS"):
        # GitHub's servers are outside Israel and several chains' price sites don't answer them.
        last = {}
        try:
            last = json.loads((ROOT / "published" / "stamp.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        notice = ("עדכון חלקי: היום המחשב בבית לא עדכן את העמוד, ולכן העדכון נעשה מהענן. חלק מהרשתות "
                  "(למשל ויקטורי, סופר ספיר / נטו חסכון, זול ובגדול, יילו, קרפור) לא זמינות מחוץ לישראל ולא נבדקו, "
                  "כך שייתכן שהסל הכי זול שונה.")
        if last.get("stamp"):
            notice += f" העדכון המלא האחרון: {last['stamp']}."
    (SITE / "index.html").write_text(render(jobs, titles, stamp, notice), encoding="utf-8")
    # Run diagnostics (no address): which chains failed and why, readable at <page>/status.txt
    lines = [f"run {stamp}"]
    for job, title in zip(jobs, titles):
        j = app.JOBS.get(job["id"], {})
        cov = job["result"].get("coverage") or {}
        lines += [f"== {title}", f"stores compared: {len(job['result'].get('stores') or [])}",
                  f"chains: {', '.join(cov.get('chains_checked') or [])}",
                  "failed: " + "; ".join(f"{x.get('chain')}: {x.get('reason')}" for x in cov.get("chains_skipped_or_failed") or []),
                  "extra chains (store lists):", str(j.get("extra_stores_log", ""))[-1500:]]
        log = app.WORK_DIR / f"job-{job['id']}.log"
        if log.exists():
            keep = [ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
                    if re.search(r"^(ok|FAIL)\s|warning|stores within|outside the radius|Traceback|Error", ln)]
            lines += ["log:"] + keep[-80:]
    addr = {e["address"] for e in cfg["lists"]}
    text = "\n".join(lines)
    for a in addr:
        text = text.replace(a, "<address>")
    (SITE / "status.txt").write_text(text, encoding="utf-8")
    if "--publish" in sys.argv:  # the PC run: a full page that the GitHub workflow publishes as is
        pub = ROOT / "published"
        pub.mkdir(exist_ok=True)
        for f in pub.iterdir():
            f.unlink()
        for f in SITE.iterdir():
            (pub / f.name).write_bytes(f.read_bytes())
        (pub / "stamp.json").write_text(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), "stamp": stamp,
                                                    "source": "pc"}, ensure_ascii=False), encoding="utf-8")
        # the PC's product decisions travel with it (new items searched on the PC)
        DATA.mkdir(parents=True, exist_ok=True)
        for name in ("queries_cache.json", "ref_products.json"):
            src = app.WORK_DIR / name
            if src.exists():
                (DATA / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"page: {SITE / 'index.html'}")


if __name__ == "__main__":
    main()
