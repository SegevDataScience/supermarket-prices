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


SEARCH_JS = r'''<script>
(() => {
  // Free search over the price data of the stores near home (prices.json, refreshed with the page).
  // Runs entirely in the browser: no server, no AI. Same ranking rule as the app.
  const $ = (id) => document.getElementById(id);
  const money = (v) => "₪" + v.toLocaleString("he-IL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const N = (s) => String(s || "").toLowerCase().replace(/["'״׳`.,()\-*\/+%!?:;]/g, " ")
    .replace(/ך/g, "כ").replace(/ם/g, "מ").replace(/ן/g, "נ").replace(/ף/g, "פ").replace(/ץ/g, "צ").replace(/\s+/g, " ").trim();
  const OTHER = /חתול|כלב|לחיות|פנסי ?פיסט|פריסקיז|ויסקס|פדיגרי|בטעמ|ממרח|צ ?יפס|פופקו|פריכ|ספרינג|משקה|מגבונ|לילד|לתינוק|תינוק|במלח|בחומצ|כבוש|חמוצ|שימור|קפוא|מיובש|מוחמצ|במילוי|ממולא|רביעי|שישי|שלישי|מארז|זוג(?![א-ת])/;
  const UNITW = /^(גרמ|גר|ג|ליטר|ל|מל|מ"ל|יח|יחידות|קג|קילו|גרמים)$/;
  const SIZE = /(\d+(?:\.\d+)?)\s*(ק"?ג|קג|קילו|גרמ|גר|ג|ליטר|ל|מ"?ל|מל|יח)(?![א-ת])/;
  let DATA = null, words = null, REFS = new Map();

  async function load() {
    if (DATA) return DATA;
    const res = await fetch("prices.json", { cache: "no-cache" });
    DATA = await res.json();
    words = DATA.p.map((p) => N(p[1]).split(" "));
    Object.entries(DATA.refs || {}).forEach(([k, v]) => REFS.set(N(k).split(" ").filter((w) => w.length > 1).join(" "), v));
    return DATA;
  }
  // short words match exactly or with a plural ending ("חלב" is not "חלבה"); longer words by their first 4 letters
  const wordOk = (q, w) => (q.length <= 3 ? w === q || w === q + "ימ" || w === q + "ות" : w.startsWith(q.slice(0, Math.min(q.length, 4))));
  const matches = (qw, pi) => qw.every((q) => words[pi].some((w) => wordOk(q, w)));
  const size = (name) => { const m = N(name).match(SIZE); return m ? m[1] + m[2][0] : null; };

  function parseLine(line) {
    let t = line.trim(), qty = null, kg = /ק"?ג|קילו/.test(t);
    let m = t.match(/^(\d+(?:\.\d+)?)\s+(.+)$/) || t.match(/^(.+?)\s+(\d+(?:\.\d+)?)\s*(?:ק"?ג|קילו|יח'?|יחידות)?$/);
    if (m) { const a = parseFloat(m[1]); if (!isNaN(a)) { qty = a; t = m[2]; } else { qty = parseFloat(m[2]); t = m[1]; } }
    t = t.replace(/ק"?ג|קילו|יחידות|יח'?/g, " ").trim();
    return { text: line.trim(), q: N(t).split(" ").filter((w) => w.length > 1), qty, kg };
  }

  function solve(item, refOverride) {
    const { p, s } = DATA;
    const cand = [];
    for (let pi = 0; pi < p.length; pi++) if (matches(item.q, pi)) cand.push(pi);
    const multi = /\d\s*[*xX×]\s*\d/;
    const otherOk = (pi) => (!OTHER.test(N(p[pi][1])) || item.q.some((q) => OTHER.test(q))) && (!multi.test(p[pi][1]) || multi.test(item.text));
    const pool = cand.filter(otherOk);
    if (!pool.length) return { cands: [], ref: null, picks: [] };
    const where = new Map(); // product -> [[store, price]]
    pool.forEach((pi) => where.set(pi, []));
    s.forEach((rows, si) => rows.forEach(([pi, price]) => { const w = where.get(pi); if (w) w.push([si, price]); }));
    const first = item.q[0];
    const score = (pi) => {
      const ws = words[pi], cov = where.get(pi).length;
      const prices = where.get(pi).map((x) => x[1]).sort((a, b) => a - b);
      const head = (ws[0] && wordOk(first, ws[0])) ? 1 : 0;
      const extra = ws.filter((w) => w.length > 1 && !/^\d/.test(w) && !UNITW.test(w) && !item.q.some((q) => wordOk(q, w))).length;
      // fresh produce by weight ("מלפפון" per kg) beats a jar sold everywhere; then the product sold in the
      // most stores (a fair comparison), the plainest name (fewest extra words), the typical (median) price
      return [head && p[pi][2] === 1 ? 2 : head, cov >= maxCov - 1 ? 1 : 0, -extra, cov, -(prices[Math.floor(prices.length / 2)] || 1e9)];
    };
    const maxCov = Math.max(...pool.map((pi) => where.get(pi).length));
    const ranked = pool.filter((pi) => where.get(pi).length).sort((a, b) => {
      const A = score(a), B = score(b);
      for (let k = 0; k < A.length; k++) if (B[k] !== A[k]) return B[k] - A[k];
      return 0;
    });
    const known = REFS.get(item.q.join(" "));
    const ref = refOverride ?? (known !== undefined && where.has(known) ? known : (known !== undefined ? (where.set(known, []), s.forEach((rows, si) => rows.forEach(([pi, price]) => { if (pi === known) where.get(known).push([si, price]); })), known) : ranked[0]));
    if (known !== undefined && !ranked.includes(known)) ranked.unshift(known);
    const refSize = size(p[ref][1]), weighted = p[ref][2] === 1;
    const refWords = new Set(words[ref]);
    let units = item.qty ?? 1;
    if (weighted && item.qty && !item.kg) units = +(item.qty * 0.15).toFixed(2); // a count of produce ≈ 150 g each
    const picks = s.map((rows, si) => {
      const exact = rows.find(([pi]) => pi === ref);
      if (exact) return { si, pi: ref, price: exact[1], sub: false };
      const opts = rows.filter(([pi]) => where.has(pi) && otherOk(pi) && (p[pi][2] === 1) === weighted);
      if (!opts.length) return null;
      const best = opts.map(([pi, price]) => ({ pi, price, same: refSize && size(p[pi][1]) === refSize ? 1 : 0,
        overlap: words[pi].filter((w) => refWords.has(w)).length }))
        .sort((a, b) => b.same - a.same || b.overlap - a.overlap || a.price - b.price)[0];
      // produce has a different internal code in every chain: the same first word ("מלפפון") is the same produce
      const refHead = words[ref][0] || "", head = words[best.pi][0] || "";
      const sameProduce = weighted && refHead && head.slice(0, 4) === refHead.slice(0, 4) && words[best.pi].length <= words[ref].length + 1;
      return { si, pi: best.pi, price: best.price, sub: !sameProduce };
    });
    const exactLines = picks.filter((x) => x && !x.sub).map((x) => x.price).sort((a, b) => a - b);
    const med = exactLines[Math.floor(exactLines.length / 2)];
    picks.forEach((x, i) => { if (x) { x.total = +(x.price * units).toFixed(2); if (x.sub && med && x.price > 2 * med) picks[i] = null; } });
    return { cands: ranked.slice(0, 8), ref, picks, units, weighted };
  }

  function run(refs = {}) {
    const lines = $("q").value.split(/\n|,/).map((l) => l.trim()).filter(Boolean);
    if (!lines.length) { $("out").innerHTML = ""; return; }
    const items = lines.map(parseLine).map((it, i) => ({ ...it, r: solve(it, refs[i]) }));
    const S = DATA.stores;
    const agg = S.map((st, si) => {
      let total = 0, found = 0, subs = 0;
      items.forEach((it) => { const x = it.r.picks[si]; if (x) { total += x.total; found++; if (x.sub) subs++; } });
      return { si, st, total: +total.toFixed(2), found, subs };
    }).filter((a) => a.found);
    agg.sort((a, b) => (a.st.kind === "convenience") - (b.st.kind === "convenience") || b.found - a.found || a.total - b.total);
    if (!agg.length) { $("out").innerHTML = `<div class=notice>לא נמצאו מוצרים כאלה בחנויות שליד הבית. נסו מילים אחרות.</div>`; return; }
    const w = agg[0], next = agg.find((a) => a !== w && a.found === w.found && a.st.kind !== "convenience");
    const medal = ["🥇", "🥈", "🥉"];
    const where = (st) => st.kind === "delivery" ? "🚚 משלוח עד הבית" : `📍 ${st.approx ? "≈ " : ""}${st.km.toFixed(1)} ק"מ`;
    const rows = agg.filter((a) => a.st.kind !== "convenience").map((a, i) => `<li class="row${a === w ? " best" : ""}"><span class=rank>${medal[i] || i + 1}</span>
      <span class=who><b>${esc(a.st.name)}</b><small>${where(a.st)}</small></span>
      <span class=price><b>${money(a.total)}</b><small class="${a.found === items.length ? "ok" : "partial"}">${a.found}/${items.length} מוצרים${a.subs ? ` · ${a.subs} תחליפים` : ""}</small></span></li>`).join("");
    const prods = items.map((it, ii) => {
      if (!it.r.ref && it.r.ref !== 0) return `<li class=prod><div class=pname>${esc(it.text)}</div><div class=pcheap>לא נמצא בחנויות שליד הבית</div></li>`;
      const opts = it.r.cands.map((pi) => `<option value="${pi}"${pi === it.r.ref ? " selected" : ""}>${esc(DATA.p[pi][1])}</option>`).join("");
      const lines = it.r.picks.map((x, si) => x ? { ...x, si } : null).filter(Boolean).sort((a, b) => a.total - b.total)
        .map((x, k) => `<div class="pline${k === 0 ? " cheap" : ""}"><span>${esc(S[x.si].name)} · ${esc(DATA.p[x.pi][1])}${x.sub ? " <i>תחליף</i>" : ""}</span><b>${money(x.total)}</b></div>`).join("");
      return `<li class=prod><div class=pname>${esc(it.text)}${it.r.weighted ? ` <small class=muted>(${it.r.units} ק"ג)</small>` : ""}</div>
        <label class=refsel>מוצר להשוואה: <select data-i="${ii}">${opts}</select></label>${lines}</li>`;
    }).join("");
    $("out").innerHTML = `
      <div class=hero><div class=hero-tag>🏆 הסל הכי זול</div><div class=hero-store>${esc(w.st.name)}</div>
        <div class=hero-where>${w.st.kind === "delivery" ? "משלוח עד הבית · לפני דמי משלוח" : where(w.st)}</div>
        <div class=hero-total>${money(w.total)}</div>
        <div class=chips><span class=chip>✔ ${w.found} מתוך ${items.length} מוצרים</span>${w.subs ? `<span class=chip>↺ ${w.subs} תחליפים</span>` : ""}
        ${next && next.total - w.total > 0.005 ? `<span class="chip good">חוסך ${money(next.total - w.total)} לעומת ${esc(next.st.name)}</span>` : ""}</div></div>
      <h3>דירוג החנויות</h3><ol class=ranking>${rows}</ol>
      <details class=more open><summary>פירוט לפי מוצר (אפשר להחליף את המוצר להשוואה)</summary><ul class=products>${prods}</ul></details>
      <p class=muted style="font-size:.8rem">ההתאמה לפי שם המוצר והברקוד, בלי בינה מלאכותית; מוצר מסומן "תחליף" כשהחנות לא מוכרת את אותו מוצר. כמות של ירקות בלי ק"ג מחושבת כ-150 גרם ליחידה.</p>`;
    $("out").querySelectorAll("select[data-i]").forEach((sel) => sel.addEventListener("change", () => {
      const cur = {}; $("out").querySelectorAll("select[data-i]").forEach((x) => { cur[+x.dataset.i] = +x.value; });
      run(cur);
    }));
  }

  $("go").addEventListener("click", async () => {
    $("go").disabled = true; $("go").textContent = "מחפש…";
    try { await load(); run(); } catch (e) { $("out").innerHTML = `<div class=notice>לא הצלחתי לטעון את המחירים (${esc(e.message)})</div>`; }
    $("go").disabled = false; $("go").textContent = "השווה מחירים";
  });
  $("clear").addEventListener("click", () => { $("q").value = ""; $("out").innerHTML = ""; $("q").focus(); });
})();
</script>
'''


def render(jobs, titles, stamp, notice=""):
    e = html.escape
    stores_all = (jobs[0]["result"].get("stores") or []) if jobs else []
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    sections, tabs = [], []
    for n, (job, title) in enumerate(zip(jobs, titles), start=1):
        r = job["result"]
        w = r.get("winner") or {}
        dates = sorted({f["date"] for f in job["price_freshness"].values() if f.get("date")})
        date_txt = ", ".join(datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m") for d in dates) or "—"
        stores = [s for s in r.get("stores") or [] if s.get("kind") != "convenience"]
        conv = [s for s in r.get("stores") or [] if s.get("kind") == "convenience"]
        same = [s for s in stores if s["store"] != w.get("store") and s.get("items_found") == w.get("items_found")
                and isinstance(s.get("total_ils"), (int, float))]
        nxt = min(same, key=lambda s: s["total_ils"]) if same else None
        saving = (nxt["total_ils"] - w["total_ils"]) if nxt and isinstance(w.get("total_ils"), (int, float)) else None
        winner_row = next((s for s in stores if s["store"] == w.get("store")), {})

        def store_row(rank, s):
            best = s["store"] == w.get("store")
            where = "🚚 משלוח עד הבית" if s.get("kind") == "delivery" else f'📍 {"≈ " if s.get("approx_location") else ""}{km(s.get("distance_km"))}'
            found = f'{s.get("items_found")}/{s.get("items_total")} מוצרים'
            subs = f' · {s["substitutes"]} תחליפים' if s.get("substitutes") else ""
            full = s.get("items_found") == s.get("items_total")
            return (f'<li class="row{" best" if best else ""}"><span class=rank>{medal.get(rank, rank)}</span>'
                    f'<span class=who><b>{e(s["store"])}</b><small>{where}</small></span>'
                    f'<span class=price><b>{money(s.get("total_ils"))}</b>'
                    f'<small class="{"ok" if full else "partial"}">{found}{subs}</small></span></li>')

        ranking = "".join(store_row(i, s) for i, s in enumerate(stores, start=1))
        conv_rows = "".join(store_row(i, s) for i, s in enumerate(conv, start=1))
        products = []
        for it in r.get("items") or []:
            picks = [p for p in it.get("picks") or [] if isinstance(p.get("line_total_ils"), (int, float))
                     and any(s["store"] == p["store"] for s in stores)]
            best = min(picks, key=lambda p: p["line_total_ils"]) if picks else None
            mine = next((p for p in it.get("picks") or [] if p["store"] == w.get("store")), None)
            here = (f'<span>{e(mine["product"])}{" <i>תחליף</i>" if mine.get("substitute") else ""}</span>'
                    f'<b>{money(mine["line_total_ils"])}</b>') if mine and mine.get("product") else '<span class=muted>לא נמצא בחנות הזולה</span><b>—</b>'
            cheap = (f'הכי זול: {e(best["store"])} · {money(best["line_total_ils"])}' if best else "לא נמצא באף חנות")
            products.append(f'<li class=prod><div class=pname>{e(it["requested"])}</div><div class=pline>{here}</div>'
                            f'<div class=pcheap>{cheap}</div></li>')
        notes = "".join(f"<li>{e(a)}</li>" for a in r.get("assumptions") or [])
        where_w = ("משלוח עד הבית · לפני דמי משלוח" if w.get("kind") == "delivery"
                   else f'{e(winner_row.get("address") or "")} · {km(w.get("distance_km"))}')
        tabs.append(f'<a href="#list{n}" class=tab>{e(title)}</a>')
        sections.append(f"""
<section class=list id=list{n}>
  <div class=list-head><h2>{e(title)}</h2><span class=pill>מחירים מ-{e(date_txt)}</span></div>
  <div class=hero>
    <div class=hero-tag>🏆 הסל הכי זול</div>
    <div class=hero-store>{e(w.get("store") or "—")}</div>
    <div class=hero-where>{where_w}</div>
    <div class=hero-total>{money(w.get("total_ils"))}</div>
    <div class=chips>
      <span class=chip>✔ {w.get("items_found", "")} מתוך {w.get("items_total", "")} מוצרים</span>
      {f'<span class=chip>↺ {w["substitutes"]} תחליפים</span>' if w.get("substitutes") else ""}
      {f'<span class="chip good">חוסך {money(saving)} לעומת {e(nxt["store"])}</span>' if saving and saving > 0 else ""}
    </div>
  </div>
  <h3>דירוג החנויות</h3>
  <ol class=ranking>{ranking}</ol>
  {f'<details class=more><summary>חנויות נוחות (סל חלקי)</summary><ol class=ranking>{conv_rows}</ol></details>' if conv_rows else ""}
  <details class=more><summary>מה קונים בחנות הזולה ({len(products)} מוצרים)</summary><ul class=products>{"".join(products)}</ul></details>
  <details class=more><summary>הערות והסברים</summary><ul class=notes>{notes}</ul></details>
  <div class=downloads><a class=btn href="list{n}-compare.xlsx" download>⬇ השוואת חנויות (אקסל)</a>
    <a class=btn href="list{n}-items.xlsx" download>⬇ כל המוצרים (אקסל)</a></div>
</section>""")
    return f"""<!doctype html><html lang=he dir=rtl><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content="noindex,nofollow">
<meta name=theme-color content="#0b1020"><title>איפה הכי זול לקנות</title>
<link rel=icon href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛒</text></svg>">
<style>
:root{{--bg:#0b1020;--bg2:#121a3a;--card:#151c3b;--card2:#1b2449;--line:#2a3463;--text:#eef1ff;--muted:#9aa3c7;
  --accent:#22d3ee;--accent2:#a78bfa;--lime:#a3e635;--warn:#fbbf24;--good:#34d399;--shadow:0 10px 30px rgb(0 0 0/.35)}}
@media (prefers-color-scheme:light){{:root{{--bg:#eef2fb;--bg2:#e3e9f8;--card:#ffffff;--card2:#f5f7fd;--line:#dfe5f3;--text:#141a33;
  --muted:#5b6485;--accent:#0891b2;--accent2:#7c3aed;--lime:#3f6212;--warn:#b45309;--good:#047857;--shadow:0 8px 24px rgb(20 26 51/.08)}}}}
*{{box-sizing:border-box}}
body{{margin:0;color:var(--text);font-family:system-ui,"Segoe UI",Rubik,Arial,sans-serif;line-height:1.45;
  background:radial-gradient(1200px 500px at 85% -10%,rgb(124 58 237/.25),transparent 60%),
             radial-gradient(900px 400px at 0% 0%,rgb(34 211 238/.18),transparent 55%),var(--bg);min-height:100vh}}
main{{max-width:720px;margin:0 auto;padding:20px 16px 40px}}
header h1{{margin:0;font-size:1.7rem;letter-spacing:-.01em}}
header h1 span{{background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}}
header p{{margin:6px 0 0;color:var(--muted);font-size:.9rem}}
.tabs{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px}}
.tab{{text-decoration:none;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 14px;font-size:.88rem}}
.notice{{background:linear-gradient(90deg,rgb(251 191 36/.18),rgb(251 191 36/.08));border:1px solid rgb(251 191 36/.45);
  color:var(--text);border-radius:14px;padding:12px 14px;margin:14px 0;font-size:.9rem}}
.list{{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:18px;margin:18px 0;box-shadow:var(--shadow)}}
.list-head{{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap}}
.list-head h2{{margin:0;font-size:1.2rem}}
.pill{{font-size:.78rem;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 10px;background:var(--card2)}}
.hero{{margin:14px 0 6px;padding:18px;border-radius:16px;background:linear-gradient(135deg,rgb(34 211 238/.16),rgb(124 58 237/.14));
  border:1px solid rgb(34 211 238/.35)}}
.hero-tag{{color:var(--accent);font-weight:700;font-size:.9rem}}
.hero-store{{font-size:1.45rem;font-weight:800;margin-top:2px}}
.hero-where{{color:var(--muted);font-size:.88rem}}
.hero-total{{font-size:2.6rem;font-weight:800;color:var(--lime);margin:8px 0 4px;font-variant-numeric:tabular-nums}}
.chips{{display:flex;flex-wrap:wrap;gap:6px}}
.chip{{font-size:.8rem;border-radius:999px;padding:4px 10px;background:var(--card2);border:1px solid var(--line)}}
.chip.good{{color:var(--good);border-color:rgb(52 211 153/.45)}}
h3{{font-size:1rem;margin:18px 0 8px;color:var(--muted);font-weight:600}}
.ranking{{list-style:none;margin:0;padding:0;display:grid;gap:8px}}
.row{{display:grid;grid-template-columns:34px 1fr auto;align-items:center;gap:10px;padding:10px 12px;border-radius:14px;
  background:var(--card2);border:1px solid var(--line)}}
.row.best{{border-color:var(--accent);box-shadow:0 0 0 1px rgb(34 211 238/.35) inset}}
.rank{{font-size:1.15rem;text-align:center;color:var(--muted);font-weight:700}}
.who b{{display:block;font-weight:650}} .who small,.price small{{display:block;color:var(--muted);font-size:.78rem}}
.price{{text-align:left;white-space:nowrap}} .price b{{font-size:1.05rem;font-variant-numeric:tabular-nums}}
.price small.ok{{color:var(--good)}} .price small.partial{{color:var(--warn)}}
details.more{{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}}
details.more summary{{cursor:pointer;font-weight:600;list-style:none}} details.more summary::before{{content:"▸ ";color:var(--accent)}}
details.more[open] summary::before{{content:"▾ "}}
.products{{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:6px}}
.prod{{padding:10px 12px;border-radius:12px;background:var(--card2);border:1px solid var(--line)}}
.pname{{font-weight:650}} .pline{{display:flex;justify-content:space-between;gap:10px;font-size:.88rem;margin-top:2px}}
.pline i{{font-style:normal;font-size:.72rem;color:var(--warn);border:1px solid rgb(251 191 36/.45);border-radius:6px;padding:0 5px;margin-inline-start:4px}}
.pcheap{{color:var(--muted);font-size:.78rem;margin-top:2px}} .muted{{color:var(--muted)}}
.notes{{color:var(--muted);font-size:.84rem;padding-inline-start:18px}}
.downloads{{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}}
.btn{{text-decoration:none;color:var(--text);background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:8px 12px;font-size:.86rem}}
.btn:hover{{border-color:var(--accent)}} .btn.primary{{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#06101f;border:0;font-weight:700}}
button.btn{{font:inherit;cursor:pointer}} button.btn:disabled{{opacity:.6}}
textarea{{width:100%;font:inherit;color:var(--text);background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:10px 12px;resize:vertical}}
textarea:focus{{outline:2px solid var(--accent);outline-offset:1px}}
.section-title{{font-size:1.1rem;margin:26px 4px 0}}
.refsel{{display:block;font-size:.8rem;color:var(--muted);margin:4px 0}} .refsel select{{max-width:100%;font:inherit;font-size:.82rem;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:2px 6px}}
.pline.cheap b{{color:var(--good)}} #out{{margin-top:6px}}
footer{{color:var(--muted);font-size:.8rem;margin:22px 4px;text-align:center}}
</style></head><body><main>
<header><h1>🛒 <span>איפה הכי זול לקנות?</span></h1>
<p>עודכן {e(stamp)} · מחירי מדף מהקבצים הרשמיים של הרשתות, בלי מבצעים ומחירי מועדון</p></header>
{f'<div class=notice>⚠ {e(notice)}</div>' if notice else ""}
<section class=list id=search>
  <div class=list-head><h2>🔎 חיפוש חופשי</h2><span class=pill>{len(stores_all)} חנויות ליד הבית</span></div>
  <p class=muted style="margin:6px 0 10px;font-size:.88rem">כתבו מוצר אחד או רשימה (מוצר בכל שורה, אפשר עם כמות: "10 מלפפונים", "חלב 2").</p>
  <textarea id=q rows=5 placeholder="למשל:&#10;חלב 3%&#10;10 מלפפונים&#10;במבה&#10;קפה טורקי"></textarea>
  <div class=downloads><button id=go class="btn primary" type=button>השווה מחירים</button><button id=clear class=btn type=button>נקה</button></div>
  <div id=out></div>
</section>
<h2 class=section-title>📋 הרשימות השמורות</h2>
{f'<nav class=tabs>{"".join(tabs)}</nav>' if len(tabs) > 1 else ""}
{"".join(sections)}
<footer>מתעדכן אוטומטית כל 3 ימים. הסל הכי זול = הסכום הנמוך ביותר לכל המוצרים יחד, מבין החנויות שיש בהן הכי הרבה מהמוצרים (כולל תחליפים).</footer>
</main>
""" + SEARCH_JS + """
</body></html>"""


def build_prices(jobs):
    """prices.json for the page's free search: every store compared for the lists (near home, all chains)
    with all its products. Real barcodes are shared across chains; chain-internal codes stay per chain."""
    today = datetime.now().strftime("%Y%m%d")
    stores, seen = [], set()
    for job in jobs:
        fresh = job["price_freshness"]
        for st in job["result"].get("stores") or []:
            ref = st.get("store_ref")
            if ref and ref not in seen:
                seen.add(ref)
                stores.append((ref, st, fresh.get(ref, {})))
    products, index, per_store, meta, names = [], {}, [], [], []
    for ref, st, f in stores:
        chain, sid = ref.split(":")
        tsv = app.CACHE_DIR / f"{chain}-{sid}-{today}.tsv"
        if not tsv.exists():
            continue
        rows = []
        for r in app._load_tsv(tsv):
            code = r[0].strip()
            key = code if code.isdigit() and len(code) >= 8 else f"{chain}:{code}"
            if key not in index:
                index[key] = len(products)
                products.append([key, r[1].strip(), 1 if r[5] == "1" else 0])
                names.append([])
            names[index[key]].append(r[1].strip())
            rows.append([index[key], round(r[2], 2)])
        per_store.append(rows)
        meta.append({"name": st["store"], "kind": st.get("kind", "store"), "km": st.get("distance_km") or 0,
                     "approx": bool(st.get("approx_location")), "date": f.get("date")})
    # One display name per product: the chains name the same barcode differently ("במבה 80 גרם אסם" /
    # "חטיף במבה קלאסית"); use the first word most chains agree on, then the most detailed such name.
    from collections import Counter
    for i, ns in enumerate(names):
        heads = Counter(n.split()[0] for n in ns if n.split())
        if heads:
            top = heads.most_common(1)[0][0]
            products[i][1] = max((n for n in ns if n.split() and n.split()[0] == top), key=len)
    # The app's reference products (decided on the PC, same barcode in every store): an item typed on the
    # page that was ever searched in the app gets exactly the same product.
    refs = {}
    raw = app._read_json(app.WORK_DIR / "ref_products.json", {})
    for k, v in raw.items():
        name = k.split("|")[0].strip()
        if isinstance(v, dict) and v.get("code") and v["code"] in index and name not in refs:
            refs[name] = index[v["code"]]
    return {"date": datetime.now().strftime("%Y-%m-%d"), "stores": meta, "p": products, "s": per_store, "refs": refs}


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
        notice = ("עדכון חלקי: המחשב בבית לא עדכן את העמוד ב-3 הימים האחרונים, ולכן העדכון נעשה מהענן. חלק מהרשתות "
                  "(למשל ויקטורי, סופר ספיר / נטו חסכון, זול ובגדול, יילו, קרפור) לא זמינות מחוץ לישראל ולא נבדקו, "
                  "כך שייתכן שהסל הכי זול שונה.")
        if last.get("stamp"):
            notice += f" העדכון המלא האחרון: {last['stamp']}."
    (SITE / "index.html").write_text(render(jobs, titles, stamp, notice), encoding="utf-8")
    (SITE / "prices.json").write_text(json.dumps(build_prices(jobs), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
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
