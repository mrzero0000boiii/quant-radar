"""Builds the static site: one self-contained HTML file plus a JSON export."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("radar.render")

# Keeps the page snappy. Anything beyond this is still written to jobs.json.
MAX_EMBEDDED = 6000

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quant Radar</title>
<style>
:root{
  --bg:#0b0d10; --panel:#12151a; --panel2:#171b21; --line:#232932;
  --fg:#e6e9ee; --dim:#8b949e; --dimmer:#5e6672;
  --accent:#4da3ff; --new:#3fb950; --warn:#d29922; --bad:#f85149; --applied:#8957e5;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:light){
  :root{--bg:#fbfcfd;--panel:#fff;--panel2:#f4f6f8;--line:#dde3ea;--fg:#12161c;
        --dim:#5a6472;--dimmer:#8b949e;--accent:#0969da;--new:#1a7f37;--applied:#6639ba;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:20;background:var(--panel);
  border-bottom:1px solid var(--line);padding:10px 16px}
.hrow{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{margin:0;font-size:15px;letter-spacing:.3px;font-weight:650}
h1 span{color:var(--accent)}
.meta{font:11px/1.4 var(--mono);color:var(--dim)}
.meta b{color:var(--fg);font-weight:600}
.legend{margin-top:7px;max-width:900px;line-height:1.6;color:var(--dimmer)}
.legend b{color:var(--dim);font-weight:600}
.mono{font-family:var(--mono)}
tr.pre .t-age:first-child{color:var(--dimmer)}
.controls{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px;align-items:center}
input[type=search],select{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
  border-radius:5px;padding:5px 8px;font:12px var(--mono);outline:none}
input[type=search]{min-width:230px;flex:1 1 230px}
input[type=search]:focus,select:focus{border-color:var(--accent)}
.chip{background:var(--panel2);border:1px solid var(--line);border-radius:99px;
  padding:4px 10px;font:11px var(--mono);color:var(--dim);cursor:pointer;user-select:none}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.chip.on.g{background:var(--new);border-color:var(--new)}
.chip.b{border-color:var(--accent);color:var(--accent)}
.chip.on.b{background:var(--accent);color:#fff}
main{padding:0 16px 60px}
table{width:100%;border-collapse:collapse;margin-top:10px}
th{position:sticky;top:0;text-align:left;font:11px var(--mono);color:var(--dim);
  font-weight:500;padding:7px 8px;border-bottom:1px solid var(--line);
  background:var(--bg);cursor:pointer;white-space:nowrap}
th:hover{color:var(--fg)}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:var(--panel2)}
.t-title{font-weight:550;max-width:440px}
.t-firm{white-space:nowrap;font-weight:550}
.t-loc{color:var(--dim);max-width:230px;font-size:12px}
.t-age{font:11px var(--mono);color:var(--dim);white-space:nowrap;text-align:right}
.tag{display:inline-block;font:10px/1.5 var(--mono);padding:1px 6px;border-radius:4px;
  border:1px solid var(--line);color:var(--dim);margin-right:4px;white-space:nowrap}
.tag.new{background:var(--new);border-color:var(--new);color:#fff;font-weight:700}
.tag.intern{color:var(--accent);border-color:var(--accent)}
.tag.applied{background:var(--applied);border-color:var(--applied);color:#fff}
.tag.lang{color:var(--warn);border-color:var(--warn)}
.tag.langhi{color:var(--bad);border-color:var(--bad)}
.empty{padding:40px 0;text-align:center;color:var(--dim)}
details{margin-top:22px;border-top:1px solid var(--line);padding-top:12px}
summary{cursor:pointer;font:12px var(--mono);color:var(--dim)}
.gaps{columns:260px;column-gap:18px;margin-top:10px;font:11px var(--mono)}
.gaps div{break-inside:avoid;padding:2px 0;color:var(--dimmer)}
#more{display:block;margin:16px auto;padding:7px 18px;background:var(--panel2);
  color:var(--fg);border:1px solid var(--line);border-radius:6px;
  font:12px var(--mono);cursor:pointer}
@media(max-width:980px){ .col-posted{display:none} }
@media(max-width:720px){
  .t-loc,.col-loc,.t-cat,.col-cat{display:none}
  .t-title{max-width:none}
  input[type=search]{min-width:100%}
  .legend{display:none}
}
</style>
</head>
<body>
<header>
  <div class="hrow">
    <h1>QUANT<span>RADAR</span></h1>
    <div class="meta">
      <b id="m-shown">0</b> shown ·
      <b id="m-target">0</b> 2027 intern/summer ·
      <b id="m-live">0</b> open total ·
      <b id="m-new">0</b> new 24h ·
      <b id="m-firms">0</b> firms ·
      updated <b>__UPDATED__</b> · next sweep ≤4h
    </div>
  </div>
  <div class="meta legend">
    <b>2027 intern / summer only</b> is on by default: internships, summer
    analyst and summer associate seats for the 2027 cycle, plus graduate roles.
    Undated internships count, since firms usually omit the year. Toggle it off
    for the full board. ·
    <b>seen</b> = when radar first caught the posting appear ·
    <b>posted</b> = the date the board itself claims ·
    <b>NEW</b> = radar watched it appear, so it is genuinely fresh.
    Postings that were already up the first time radar read a board show
    <span class="mono">≥</span> and never get a NEW badge or an email.
  </div>
  <div class="controls">
    <input type="search" id="q" placeholder="search title, firm, location…  (/ to focus)">
    <select id="f-level">
      <option value="">any level</option>
      <option value="intern">intern</option>
      <option value="newgrad">new grad</option>
      <option value="entry">entry</option>
      <option value="unknown">unknown</option>
      <option value="experienced">experienced</option>
    </select>
    <select id="f-cat">
      <option value="">any role</option>
    </select>
    <select id="f-region">
      <option value="">any region</option>
    </select>
    <select id="f-firmcat">
      <option value="">any firm type</option>
      <option value="prop">prop / MM</option>
      <option value="fund">hedge fund / AM</option>
      <option value="bank">bank</option>
      <option value="exchange">exchange / infra</option>
      <option value="crypto">crypto</option>
      <option value="fintech">fintech</option>
    </select>
    <span class="chip b" id="c-target">2027 intern / summer only</span>
    <span class="chip g" id="c-new">new 24h</span>
    <span class="chip" id="c-lang">hide language-risk</span>
    <span class="chip" id="c-applied">hide applied</span>
    <span class="chip" id="c-pre">hide pre-existing</span>
  </div>
</header>
<main>
  <table>
    <thead><tr>
      <th data-sort="first_seen">seen</th>
      <th data-sort="posted_at" class="col-posted">posted</th>
      <th data-sort="firm">firm</th>
      <th data-sort="title">role</th>
      <th data-sort="category" class="col-cat">type</th>
      <th data-sort="location" class="col-loc">location</th>
      <th data-sort="score">rank</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" hidden>nothing matches those filters</div>
  <button id="more" hidden>load more</button>

  <details>
    <summary id="gapsum">boards that could not be read automatically — check these by hand</summary>
    <div class="gaps" id="gaps"></div>
  </details>
</main>
<script>
const JOBS = __JOBS__;
const GAPS = __GAPS__;
const PAGE_SIZE = 200;

const $ = s => document.querySelector(s);
// targetOnly defaults ON: the board exists to surface 2027 intern / summer
// analyst / summer associate seats. Toggle it off to see everything.
const state = {sort:"first_seen", dir:-1, limit:PAGE_SIZE, targetOnly:true,
               newOnly:false, hideLang:false, hideApplied:true, hidePre:false};

function ago(iso){
  if(!iso) return "—";
  const m = (Date.now() - new Date(iso)) / 60000;
  if(m < 60) return Math.max(0,Math.round(m)) + "m";
  if(m < 1440) return Math.round(m/60) + "h";
  if(m < 43200) return Math.round(m/1440) + "d";
  return Math.round(m/43200) + "mo";
}
function esc(s){ return (s||"").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function fillOptions(){
  const cats = [...new Set(JOBS.map(j=>j.category))].sort();
  for(const c of cats) $("#f-cat").add(new Option(c, c));
  const regs = [...new Set(JOBS.flatMap(j=>j.regions||[]))].sort();
  for(const r of regs) $("#f-region").add(new Option(r, r));
}

function matches(j){
  const q = $("#q").value.trim().toLowerCase();
  if(q){
    const hay = (j.title+" "+j.firm+" "+j.location+" "+j.category+" "+(j.department||"")).toLowerCase();
    if(!q.split(/\s+/).every(t => hay.includes(t))) return false;
  }
  const lv = $("#f-level").value; if(lv && j.level !== lv) return false;
  const ct = $("#f-cat").value; if(ct && j.category !== ct) return false;
  const rg = $("#f-region").value; if(rg && !(j.regions||[]).includes(rg)) return false;
  const fc = $("#f-firmcat").value; if(fc && j.firm_category !== fc) return false;
  if(state.targetOnly && !j.target) return false;
  if(state.newOnly && !j.is_new_24h) return false;
  if(state.hideLang && j.language_risk === "high") return false;
  if(state.hideApplied && j.applied) return false;
  if(state.hidePre && j.detection && j.detection !== "live") return false;
  return true;
}

function render(){
  const rows = JOBS.filter(matches).sort((a,b)=>{
    const k = state.sort;
    let av = a[k], bv = b[k];
    if(k === "score"){ av = a.score; bv = b.score; }
    if(typeof av === "string"){ return av.localeCompare(bv||"") * state.dir; }
    return ((av||0) > (bv||0) ? 1 : (av||0) < (bv||0) ? -1 : 0) * state.dir;
  });

  const slice = rows.slice(0, state.limit);
  $("#rows").innerHTML = slice.map(j => {
    const tags = [];
    if(j.is_new_24h) tags.push('<span class="tag new">NEW</span>');
    if(j.level === "intern") tags.push('<span class="tag intern">intern</span>');
    else if(j.level === "newgrad") tags.push('<span class="tag">new grad</span>');
    else if(j.level === "experienced") tags.push('<span class="tag">exp</span>');
    if(j.cycle) tags.push('<span class="tag'+(j.cycle>="2027"?" intern":"")+'">'+j.cycle+'</span>');
    if(j.summer) tags.push('<span class="tag">summer</span>');
    if(j.applied) tags.push('<span class="tag applied">'+esc(j.applied_status||"applied")+'</span>');
    if(j.language_risk === "high") tags.push('<span class="tag langhi">lang</span>');
    else if(j.language_risk === "medium") tags.push('<span class="tag lang">lang?</span>');

    // "≥" means: it was already up when radar first read this board, so the
    // seen age is a floor, not the posting's real age.
    const pre = j.detection && j.detection !== "live";
    const seenTitle = pre
      ? (j.detection === "baseline"
          ? "Already on the board the first time radar read it - real age unknown"
          : "Detected this run, but the board says it had been up a while")
      : "Radar watched this posting appear";
    return `<tr${pre ? ' class="pre"' : ''}>
      <td class="t-age" title="${seenTitle}">${pre ? "≥" : ""}${ago(j.first_seen)}</td>
      <td class="t-age col-posted">${j.posted_at ? ago(j.posted_at) : "—"}</td>
      <td class="t-firm">${esc(j.firm)}</td>
      <td class="t-title">${j.url ? `<a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>` : esc(j.title)}<br>${tags.join("")}</td>
      <td class="t-cat"><span class="tag">${esc(j.category)}</span></td>
      <td class="t-loc">${esc(j.location) || "—"}</td>
      <td class="t-age">${j.score}</td>
    </tr>`;
  }).join("");

  $("#empty").hidden = rows.length > 0;
  $("#more").hidden = rows.length <= state.limit;
  $("#more").textContent = `load more (${rows.length - slice.length} hidden)`;
  $("#m-live").textContent = JOBS.length;
  $("#m-target").textContent = JOBS.filter(j=>j.target).length;
  $("#m-new").textContent = JOBS.filter(j=>j.is_new_24h).length;
  $("#m-firms").textContent = new Set(JOBS.map(j=>j.firm)).size;
  $("#m-shown").textContent = rows.length;
}

function bind(){
  for(const el of ["#q","#f-level","#f-cat","#f-region","#f-firmcat"])
    $(el).addEventListener("input", ()=>{state.limit=PAGE_SIZE; render();});
  const toggle = (sel, key) => $(sel).addEventListener("click", e=>{
    state[key] = !state[key]; e.target.classList.toggle("on", state[key]);
    state.limit = PAGE_SIZE; render();
  });
  toggle("#c-target","targetOnly"); toggle("#c-new","newOnly");
  toggle("#c-lang","hideLang"); toggle("#c-applied","hideApplied");
  toggle("#c-pre","hidePre");
  $("#c-applied").classList.add("on");
  $("#c-target").classList.add("on");
  document.querySelectorAll("th[data-sort]").forEach(th =>
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      state.dir = (state.sort === k) ? -state.dir : -1;
      state.sort = k; render();
    }));
  $("#more").addEventListener("click", ()=>{state.limit += PAGE_SIZE*2; render();});
  document.addEventListener("keydown", e=>{
    if(e.key === "/" && document.activeElement.tagName !== "INPUT"){e.preventDefault();$("#q").focus();}
  });
}

$("#gaps").innerHTML = GAPS.map(g =>
  `<div>${esc(g.name)} <a href="${esc(g.url)}" target="_blank" rel="noopener">↗</a></div>`).join("");
$("#gapsum").textContent =
  `${GAPS.length} boards could not be read automatically — check these by hand`;

fillOptions(); bind(); render();
</script>
</body>
</html>
"""


def careers_guess(firm: dict) -> str:
    slug = (firm.get("slugs") or [firm["name"]])[0]
    return f"https://www.google.com/search?q={slug}+careers+internship"


def build(site_dir: Path, jobs: list[dict], gaps: list[dict], *,
          stats: dict | None = None) -> Path:
    site_dir.mkdir(parents=True, exist_ok=True)
    ranked = sorted(jobs, key=lambda x: x.get("score", 0), reverse=True)
    truncated = max(0, len(ranked) - MAX_EMBEDDED)
    if truncated:
        log.warning("embedding the top %d of %d postings by rank (page size limit); "
                    "the full set is in jobs.json", MAX_EMBEDDED, len(ranked))
        ranked = ranked[:MAX_EMBEDDED]

    slim = [{k: j.get(k) for k in (
        "id", "firm", "firm_category", "title", "location", "url", "category",
        "level", "cycle", "language_risk", "regions", "score", "first_seen",
        "posted_at", "applied", "applied_status", "department", "is_new_24h",
        "detection", "target", "summer")} for j in ranked]

    html = (PAGE
            .replace("__JOBS__", json.dumps(slim, separators=(",", ":")))
            .replace("__GAPS__", json.dumps(gaps, separators=(",", ":")))
            .replace("__UPDATED__",
                     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")))

    out = site_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    full = [{k: v for k, v in j.items() if not k.startswith("_")} for j in jobs]
    (site_dir / "jobs.json").write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                    "stats": stats or {}, "embedded": len(slim),
                    "truncated": truncated, "jobs": full}, indent=1), encoding="utf-8")
    (site_dir / ".nojekyll").write_text("")
    log.info("wrote %s (%d jobs, %.0f KB)", out, len(slim), out.stat().st_size / 1024)
    return out
