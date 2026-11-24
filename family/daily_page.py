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
  // Free search: pick a locality + street (closed lists), a radius and a shopping list. Runs in the
  // browser on the price files in data/ (refreshed from the home PC). No server, no AI.
  const $ = (id) => document.getElementById(id);
  const money = (v) => "₪" + v.toLocaleString("he-IL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const N = (s) => String(s || "").toLowerCase().replace(/["'״׳`.,()\-*\/+%!?:;]/g, " ")
    .replace(/ך/g, "כ").replace(/ם/g, "מ").replace(/ן/g, "נ").replace(/ף/g, "פ").replace(/ץ/g, "צ").replace(/\s+/g, " ").trim();
  const OTHER = /חתול|כלב|לחיות|פנסי ?פיסט|פריסקיז|ויסקס|פדיגרי|בטעמ|ממרח|צ ?יפס|פופקו|פריכ|ספרינג|משקה|מגבונ|לילד|לתינוק|תינוק|במלח|בחומצ|כבוש|חמוצ|שימור|קפוא|מיובש|מוחמצ|במילוי|ממולא|רביעי|שישי|שלישי|מארז|זוג(?![א-ת])/;
  const UNITW = /^(גרמ|גר|ג|ליטר|ל|מל|מ"ל|יח|יחידות|קג|קילו|גרמים)$/;
  const SIZE = /(\d+(?:\.\d+)?)\s*(ק"?ג|קג|קילו|גרמ|גר|ג|ליטר|ל|מ"?ל|מל|יח)(?![א-ת])/;
  const km = (a, b) => { const r = Math.PI / 180, dl = (b[0] - a[0]) * r, dn = (b[1] - a[1]) * r;
    const h = Math.sin(dl / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dn / 2) ** 2; return 12742 * Math.asin(Math.sqrt(h)); };
  const get = async (u) => { const r = await fetch(u, { cache: "no-cache" }); if (!r.ok) throw new Error(u + " " + r.status); return r.json(); };

  let PLACES = [], STORES = null, PRODUCTS = null, WORDS = null, REFS = new Map(), STREETS = [];
  const fileCache = new Map();
  let DATA = null;

  // ---------- address ----------
  async function init() {
    try {
      PLACES = await get("data/places.json");
      $("cities").innerHTML = PLACES.map((p) => `<option value="${esc(p.n)}">`).join("");
      const meta = await get("data/stores.json");
      STORES = meta.stores;
      $("datapill").textContent = `${PLACES.length} יישובים · ${STORES.filter((s) => s.kind !== "delivery").length} סניפים · מחירים מ-${meta.date.split("-").reverse().join(".")}`;
    } catch (e) { $("datapill").textContent = "לא נטענו נתוני המחירים"; }
  }
  const place = () => PLACES.find((p) => p.n === $("city").value.trim());
  $("city").addEventListener("change", async () => {
    const p = place();
    $("street").value = ""; $("streets").innerHTML = ""; STREETS = [];
    $("street").disabled = !p; $("street").placeholder = p ? "רחוב (לא חובה)" : "בחרו יישוב קודם";
    if (!p) return;
    try { STREETS = await get(`data/streets/${p.c}.json`); $("streets").innerHTML = STREETS.map((s) => `<option value="${esc(s)}">`).join(""); } catch {}
  });
  async function homePoint(p) {
    const st = $("street").value.trim(), num = $("num").value.trim();
    if (!st) return { xy: [p.la, p.lo], label: p.n };
    try {
      const q = `${st}${num ? " " + num : ""}, ${p.n}`;
      const r = await (await fetch(`https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=il&q=${encodeURIComponent(q)}`)).json();
      if (r && r[0]) { const xy = [+r[0].lat, +r[0].lon]; if (km(xy, [p.la, p.lo]) < 12) return { xy, label: q }; }
    } catch {}
    return { xy: [p.la, p.lo], label: `${p.n} (מרכז היישוב - הרחוב לא אותר במפה)` };
  }

  // ---------- matching (same rules as before) ----------
  const wordOk = (q, w) => (q.length <= 3 ? w === q || w === q + "ימ" || w === q + "ות" : w.startsWith(q.slice(0, Math.min(q.length, 4))));
  const size = (name) => { const m = N(name).match(SIZE); return m ? m[1] + m[2][0] : null; };
  // ---------- understanding messy text ----------
  let VOCAB = null, PREF4 = null;
  function vocab() {
    if (VOCAB) return;
    VOCAB = new Set(); PREF4 = new Set();
    WORDS.forEach((ws) => ws.forEach((w) => { if (w.length > 1) { VOCAB.add(w); if (w.length >= 4) PREF4.add(w.slice(0, 4)); } }));
  }
  const known = (w) => (w.length <= 3 ? VOCAB.has(w) || VOCAB.has(w + "ימ") || VOCAB.has(w + "ות") : PREF4.has(w.slice(0, 4)));
  const STOP = new Set(["של", "עם", "בלי", "גם", "את", "או", "וגם", "קצת", "הרבה", "כמה", "יחידה", "יחידות", "חבילה", "חבילות", "אריזה", "בקבוק"]);
  // "האורז" -> "אורז", "ולחם" -> "לחם", "אורזז" -> "אורז"; null when nothing like it is sold anywhere
  function fixWord(w) {
    if (STOP.has(w)) return "";
    // "וחלב" / "החלב" -> "חלב": the conjunction ו and the article ה come first, even if "וחלב" appears in some name
    if (/^[וה]/.test(w) && w.length >= 3) { const r = w.slice(1); if (VOCAB.has(r) || (r.length >= 4 && known(r))) return r; }
    if (/^(וה|ול|וב|ומ|וש)/.test(w) && w.length >= 4) { const r = w.slice(2); if (VOCAB.has(r) || (r.length >= 4 && known(r))) return r; }
    if (VOCAB.has(w)) return w;  // the word exactly as sold ("מלח")
    for (let k = 1; k <= 2 && k < w.length - 1; k++) {  // "האורז" -> "אורז" even if some product word starts with "האור"
      if (!/^[והבלמכש]+$/.test(w.slice(0, k))) break;
      const r = w.slice(k);
      if (r.length >= 2 && (VOCAB.has(r) || (r.length >= 3 && known(r)))) return r;
    }
    if (known(w)) return w;
    if (w.length >= 4) {
      for (let i = 0; i < w.length; i++) { const r = w.slice(0, i) + w.slice(i + 1); if (known(r)) return r; }   // one extra letter
      for (let i = 0; i < w.length - 1; i++) { const r = w.slice(0, i) + w[i + 1] + w[i] + w.slice(i + 2); if (known(r)) return r; } // swapped letters
    }
    return null;
  }

  function parseLine(line) {
    let t = line.trim(), qty = null, kg = /ק"?ג|קילו/.test(t);
    let m = t.match(/^(\d+(?:\.\d+)?)\s+(.+)$/) || t.match(/^(.+?)\s+(\d+(?:\.\d+)?)\s*(?:ק"?ג|קילו|יח'?|יחידות)?$/);
    if (m) { const a = parseFloat(m[1]); if (!isNaN(a)) { qty = a; t = m[2]; } else { qty = parseFloat(m[2]); t = m[1]; } }
    t = t.replace(/ק"?ג|קילו|יחידות|יח'?/g, " ").trim();
    const raw = N(t).split(" ").filter((w) => w.length > 1);
    const fixed = raw.map(fixWord);
    const q = fixed.filter((w) => w);  // "" = filler word, null = not understood
    const bad = !q.length || fixed.filter((w) => w === null).length > q.length;
    return { text: line.trim(), q: bad ? [] : q, qty, kg, bad };
  }
  function solve(item, refOverride) {
    const p = PRODUCTS, s = DATA.s;
    const multi = /\d\s*[*xX×]\s*\d/;
    const otherOk = (pi) => (!OTHER.test(N(p[pi][1])) || item.q.some((q) => OTHER.test(q))) && (!multi.test(p[pi][1]) || multi.test(item.text));
    const where = new Map();
    for (let pi = 0; pi < p.length; pi++) if (item.q.every((q) => WORDS[pi].some((w) => wordOk(q, w))) && otherOk(pi)) where.set(pi, []);
    const known = REFS.get(item.q.join(" "));
    if (known !== undefined && !where.has(known)) where.set(known, []);
    s.forEach((rows, si) => rows.forEach(([pi, price]) => { const w = where.get(pi); if (w) w.push([si, price]); }));
    const pool = [...where.keys()].filter((pi) => where.get(pi).length);
    if (!pool.length) return { cands: [], ref: null, picks: [] };
    const first = item.q[0], maxCov = Math.max(...pool.map((pi) => where.get(pi).length));
    const score = (pi) => {
      const ws = WORDS[pi], cov = where.get(pi).length, prices = where.get(pi).map((x) => x[1]).sort((a, b) => a - b);
      const head = (ws[0] && wordOk(first, ws[0])) ? 1 : 0;
      const extra = ws.filter((w) => w.length > 1 && !/^\d/.test(w) && !UNITW.test(w) && !item.q.some((q) => wordOk(q, w))).length;
      return [head && p[pi][2] === 1 ? 2 : head, cov >= maxCov - 1 ? 1 : 0, -extra, cov, -(prices[Math.floor(prices.length / 2)] || 1e9)];
    };
    const ranked = pool.sort((a, b) => { const A = score(a), B = score(b); for (let k = 0; k < A.length; k++) if (B[k] !== A[k]) return B[k] - A[k]; return 0; });
    if (known !== undefined && where.get(known).length) { ranked.splice(ranked.indexOf(known), 1); ranked.unshift(known); }
    const ref = refOverride ?? ranked[0];
    const refSize = size(p[ref][1]), weighted = p[ref][2] === 1, refWords = new Set(WORDS[ref]);
    let units = item.qty ?? 1;
    if (weighted && item.qty && !item.kg) units = +(item.qty * 0.15).toFixed(2);
    const picks = s.map((rows) => {
      const exact = rows.find(([pi]) => pi === ref);
      if (exact) return { pi: ref, price: exact[1], sub: false };
      const opts = rows.filter(([pi]) => where.has(pi) && (p[pi][2] === 1) === weighted);
      if (!opts.length) return null;
      const best = opts.map(([pi, price]) => ({ pi, price, same: refSize && size(p[pi][1]) === refSize ? 1 : 0,
        overlap: WORDS[pi].filter((w) => refWords.has(w)).length })).sort((a, b) => b.same - a.same || b.overlap - a.overlap || a.price - b.price)[0];
      const rh = WORDS[ref][0] || "", h = WORDS[best.pi][0] || "";
      const sameProduce = weighted && rh && h.slice(0, 4) === rh.slice(0, 4) && WORDS[best.pi].length <= WORDS[ref].length + 1;
      return { pi: best.pi, price: best.price, sub: !sameProduce };
    });
    const ex = picks.filter((x) => x && !x.sub).map((x) => x.price).sort((a, b) => a - b), med = ex[Math.floor(ex.length / 2)];
    picks.forEach((x, i) => { if (x) { x.total = +(x.price * units).toFixed(2); if (x.sub && med && x.price > 2 * med) picks[i] = null; } });
    return { cands: ranked.slice(0, 8), ref, picks, units, weighted };
  }

  function render(items, home) {
    const S = DATA.stores;
    const agg = S.map((st, si) => {
      let total = 0, found = 0, subs = 0;
      items.forEach((it) => { const x = it.r.picks[si]; if (x) { total += x.total; found++; if (x.sub) subs++; } });
      return { si, st, total: +total.toFixed(2), found, subs };
    }).filter((a) => a.found);
    agg.sort((a, b) => (a.st.kind === "convenience") - (b.st.kind === "convenience") || b.found - a.found || a.total - b.total);
    if (!agg.length) { $("out").innerHTML = `<div class=notice>לא נמצאו מוצרים כאלה בחנויות ברדיוס שנבחר. נסו רדיוס גדול יותר או מילים אחרות.</div>`; return; }
    const w = agg[0], next = agg.find((a) => a !== w && a.found === w.found && a.st.kind !== "convenience");
    const medal = ["🥇", "🥈", "🥉"];
    const where = (st) => st.kind === "delivery" ? "🚚 משלוח עד הבית (יש לוודא שמגיעים לכתובת)" : `📍 ${st.approx ? "≈ " : ""}${st.km.toFixed(1)} ק"מ · ${esc(st.addr)}`;
    const old = [...new Set(S.map((st) => st.date))].sort();
    const rows = agg.filter((a) => a.st.kind !== "convenience").map((a, i) => `<li class="row${a === w ? " best" : ""}"><span class=rank>${medal[i] || i + 1}</span>
      <span class=who><b>${esc(a.st.name)}</b><small>${where(a.st)}</small></span>
      <span class=price><b>${money(a.total)}</b><small class="${a.found === items.length ? "ok" : "partial"}">${a.found}/${items.length} מוצרים${a.subs ? ` · ${a.subs} תחליפים` : ""}</small></span></li>`).join("");
    const prods = items.map((it, ii) => {
      if (it.r.ref === null || it.r.ref === undefined) return `<li class=prod><div class=pname>${esc(it.text)}</div><div class=pcheap>לא נמצא בחנויות ברדיוס</div></li>`;
      const opts = it.r.cands.map((pi) => `<option value="${pi}"${pi === it.r.ref ? " selected" : ""}>${esc(PRODUCTS[pi][1])}</option>`).join("");
      const lines = it.r.picks.map((x, si) => x ? { ...x, si } : null).filter(Boolean).sort((a, b) => a.total - b.total)
        .map((x, k) => `<div class="pline${k === 0 ? " cheap" : ""}"><span>${esc(S[x.si].name)} · ${esc(PRODUCTS[x.pi][1])}${x.sub ? " <i>תחליף</i>" : ""}</span><b>${money(x.total)}</b></div>`).join("");
      return `<li class=prod><div class=pname>${esc(it.text)}${it.r.weighted ? ` <small class=muted>(${it.r.units} ק"ג)</small>` : ""}</div>
        <label class=refsel>מוצר להשוואה: <select data-i="${ii}">${opts}</select></label>${lines}</li>`;
    }).join("");
    $("out").innerHTML = `
      <p class=muted style="font-size:.85rem;margin:10px 0 0">📍 ${esc(home.label)} · ${S.filter((x) => x.kind !== "delivery").length} סניפים ברדיוס · מחירים מ-${esc(old.map((d) => d.split("-").reverse().slice(0, 2).join(".")).join(", "))}</p>
      <div class=hero><div class=hero-tag>🏆 הסל הכי זול</div><div class=hero-store>${esc(w.st.name)}</div>
        <div class=hero-where>${where(w.st)}</div><div class=hero-total>${money(w.total)}</div>
        <div class=chips><span class=chip>✔ ${w.found} מתוך ${items.length} מוצרים</span>${w.subs ? `<span class=chip>↺ ${w.subs} תחליפים</span>` : ""}
        ${next && next.total - w.total > 0.005 ? `<span class="chip good">חוסך ${money(next.total - w.total)} לעומת ${esc(next.st.name)}</span>` : ""}</div></div>
      <h3>דירוג החנויות</h3><ol class=ranking>${rows}</ol>
      <details class=more open><summary>פירוט לפי מוצר (אפשר להחליף את המוצר להשוואה)</summary><ul class=products>${prods}</ul></details>
      <p class=muted style="font-size:.8rem">ההתאמה לפי שם המוצר והברקוד (מוצרים שחופשו באפליקציה - בדיוק כמו באפליקציה); "תחליף" = החנות לא מוכרת את אותו מוצר. ירקות בלי ק"ג נספרים כ-150 גרם ליחידה. מרחקים בקו אוויר.</p>`;
    $("out").querySelectorAll("select[data-i]").forEach((sel) => sel.addEventListener("change", () => {
      const cur = {}; $("out").querySelectorAll("select[data-i]").forEach((x) => { cur[+x.dataset.i] = +x.value; });
      const box = $("out").querySelector(".notice"); const keep = box ? box.outerHTML : "";
      render(items.map((it, i) => ({ ...it, r: solve(it, cur[i]) })), home);
      if (keep) $("out").insertAdjacentHTML("afterbegin", keep);
    }));
  }

  async function search() {
    const p = place();
    if (!p) { $("out").innerHTML = `<div class=notice>בחרו יישוב מהרשימה.</div>`; $("city").focus(); return; }
    if ($("street").value.trim() && !STREETS.includes($("street").value.trim())) { $("out").innerHTML = `<div class=notice>בחרו רחוב מהרשימה (או השאירו ריק).</div>`; return; }
    const lines = $("q").value.split(/\n|,/).map((l) => l.trim()).filter(Boolean);
    if (!lines.length) { $("out").innerHTML = `<div class=notice>כתבו לפחות מוצר אחד.</div>`; return; }
    const home = await homePoint(p), radius = +$("rad").value;
    if (!PRODUCTS) {
      PRODUCTS = await get("data/products.json");
      WORDS = PRODUCTS.map((x) => N(x[1]).split(" "));
      Object.entries(await get("data/refs.json")).forEach(([k, v]) => REFS.set(N(k).split(" ").filter((w) => w.length > 1).join(" "), v));
    }
    const chosen = STORES.map((st, i) => ({ ...st, i, km: st.kind === "delivery" ? 0 : km(home.xy, [st.la, st.lo]) }))
      .filter((st) => st.kind === "delivery" || st.km <= radius + (st.approx ? -1 : 0))
      .sort((a, b) => (a.kind === "delivery") - (b.kind === "delivery") || a.km - b.km).slice(0, 32);
    const rows = await Promise.all(chosen.map(async (st) => { if (!fileCache.has(st.i)) fileCache.set(st.i, await get(`data/s/${st.i}.json`)); return fileCache.get(st.i); }));
    DATA = { stores: chosen, s: rows };
    vocab();
    const parsed = lines.map(parseLine), wrong = parsed.filter((it) => it.bad), good = parsed.filter((it) => !it.bad);
    const wrongBox = wrong.length ? `<div class=notice>המלל שגוי: ${wrong.map((it) => `"${esc(it.text)}"`).join(", ")} - לא זוהה מוצר כזה. בדקו את הכתיב ונסו שוב.</div>` : "";
    if (!good.length) { $("out").innerHTML = wrongBox; return; }
    render(good.map((it) => ({ ...it, r: solve(it) })), home);
    if (wrongBox) $("out").insertAdjacentHTML("afterbegin", wrongBox);
  }

  $("go").addEventListener("click", async () => {
    $("go").disabled = true; $("go").textContent = "מחפש…";
    try { await search(); } catch (e) { $("out").innerHTML = `<div class=notice>שגיאה: ${esc(e.message)}</div>`; }
    $("go").disabled = false; $("go").textContent = "השווה מחירים";
  });
  $("clear").addEventListener("click", () => { $("q").value = ""; $("out").innerHTML = ""; $("q").focus(); });
  init();
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
.addr{{display:grid;grid-template-columns:2fr 2fr 1fr 1fr;gap:8px;margin-top:10px}} .addr label{{font-size:.8rem;color:var(--muted);display:grid;gap:4px}}
.addr input,.addr select{{font:inherit;font-size:.95rem;color:var(--text);background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:8px 10px;width:100%}}
.addr input:disabled{{opacity:.55}} @media (max-width:560px){{.addr{{grid-template-columns:1fr 1fr}}}}
.refsel{{display:block;font-size:.8rem;color:var(--muted);margin:4px 0}} .refsel select{{max-width:100%;font:inherit;font-size:.82rem;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:2px 6px}}
.pline.cheap b{{color:var(--good)}} #out{{margin-top:6px}}
footer{{color:var(--muted);font-size:.8rem;margin:22px 4px;text-align:center}}
</style></head><body><main>
<header><h1>🛒 <span>איפה הכי זול לקנות?</span></h1>
<p>עודכן {e(stamp)} · מחירי מדף מהקבצים הרשמיים של הרשתות, בלי מבצעים ומחירי מועדון</p></header>
{f'<div class=notice>⚠ {e(notice)}</div>' if notice else ""}
<section class=list id=search>
  <div class=list-head><h2>🔎 חיפוש חופשי</h2><span class=pill id=datapill>טוען נתונים…</span></div>
  <div class=addr>
    <label>עיר / יישוב<input id=city list=cities autocomplete=off placeholder="התחילו להקליד ובחרו"></label>
    <label>רחוב<input id=street list=streets autocomplete=off disabled placeholder="בחרו יישוב קודם"></label>
    <label>מספר<input id=num inputmode=numeric maxlength=5 placeholder="לא חובה"></label>
    <label>רדיוס<select id=rad><option value=2>2 ק"מ</option><option value=3>3 ק"מ</option><option value=4 selected>4 ק"מ</option><option value=6>6 ק"מ</option><option value=8>8 ק"מ</option><option value=12>12 ק"מ</option></select></label>
  </div>
  <datalist id=cities></datalist><datalist id=streets></datalist>
  <p class=muted style="margin:10px 0 6px;font-size:.88rem">מה קונים? מוצר בכל שורה, אפשר עם כמות ("10 מלפפונים", "חלב 2").</p>
  <textarea id=q rows=5 placeholder="למשל:&#10;חלב 3%&#10;10 מלפפונים&#10;במבה&#10;קפה טורקי"></textarea>
  <div class=downloads><button id=go class="btn primary" type=button>השווה מחירים</button><button id=clear class=btn type=button>נקה</button></div>
  <div id=out></div>
</section>
{("<h2 class=section-title>📋 הרשימות השמורות</h2>" + (f'<nav class=tabs>{"".join(tabs)}</nav>' if len(tabs) > 1 else "") + "".join(sections)) if sections else ""}
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
    # The family page always opens blank: no one's saved lists or searches are on it. The saved lists
    # (family/config.json) are computed only with --with-lists, for the owner's own use.
    cfg = load_config() if "--with-lists" in sys.argv else {"lists": []}
    seed_decisions()
    SITE.mkdir(parents=True, exist_ok=True)
    for old in SITE.glob("list*.xlsx"):
        old.unlink()
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
    if "--publish" in sys.argv:  # the PC run: stamp for the page (pushed to the "pages" branch by the .bat)
        (SITE / "stamp.json").write_text(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), "stamp": stamp,
                                                     "source": "pc"}, ensure_ascii=False), encoding="utf-8")
    print(f"page: {SITE / 'index.html'}")


if __name__ == "__main__":
    main()
