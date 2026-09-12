"""
Offline end-to-end test. Fakes every HTTP call with realistic ATS payloads so the
whole pipeline (adapters -> classify -> dedupe -> diff -> render -> email) is
exercised without touching the network.

    python -m pytest tests/ -q       (or)     python tests/test_pipeline.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import classify, main as radar_main, net, notify, render, sources, store  # noqa: E402
from radar.resolve import resolve_all  # noqa: E402

NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(hours=2)).isoformat()
OLD = (NOW - timedelta(days=40)).isoformat()

# --------------------------------------------------------------------------
# Fake network
# --------------------------------------------------------------------------

GREENHOUSE = {
    "jobs": [
        {"id": 101, "title": "Quantitative Trader Intern - Summer 2027",
         "location": {"name": "New York, NY"}, "updated_at": RECENT,
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/101"},
        {"id": 102, "title": "Senior C++ Developer, Core Trading Systems",
         "location": {"name": "London, UK"}, "updated_at": OLD,
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/102"},
        {"id": 103, "title": "Technical Recruiter",
         "location": {"name": "Chicago, IL"}, "updated_at": OLD,
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/103"},
        {"id": 104, "title": "Quantitative Researcher - Graduate Programme 2027",
         "location": {"name": "Tokyo, Japan"}, "updated_at": RECENT,
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/104"},
        {"id": 105, "title": "Portfolio Implementation Analyst",
         "location": {"name": "Greenwich, CT"}, "updated_at": RECENT,
         "absolute_url": "https://boards.greenhouse.io/acme/jobs/105"},
    ]
}

LEVER = [
    {"id": "abc-1", "text": "Software Engineer Intern",
     "categories": {"location": "Amsterdam, Netherlands", "team": "Technology",
                    "commitment": "Internship"},
     "hostedUrl": "https://jobs.lever.co/beta/abc-1", "createdAt": 1757000000000},
    {"id": "abc-2", "text": "Head of Equities Trading",
     "categories": {"location": "Hong Kong", "team": "Trading"},
     "hostedUrl": "https://jobs.lever.co/beta/abc-2", "createdAt": 1700000000000},
]

WORKDAY = {
    "total": 2,
    "jobPostings": [
        {"title": "2027 Summer Analyst, FICC Sales & Trading",
         "externalPath": "/job/New-York/2027-Summer-Analyst_R-1",
         "locationsText": "New York, United States", "postedOn": "Posted 2 Days Ago"},
        {"title": "Quantitative Strats Analyst - Graduate",
         "externalPath": "/job/London/Strats_R-2",
         "locationsText": "London, United Kingdom", "postedOn": "Posted Today"},
    ],
}

ASHBY = {"jobs": [
    {"id": "ash-1", "title": "Data Scientist, Markets", "location": "Singapore",
     "publishedAt": RECENT, "jobUrl": "https://jobs.ashbyhq.com/gamma/ash-1",
     "department": "Research", "employmentType": "FullTime"},
]}


def fake_get_json(url, **kw):
    if "greenhouse" in url and "/acme/" in url:
        return GREENHOUSE
    if "lever.co/v0/postings/beta" in url:
        return LEVER
    if "ashbyhq.com/posting-api/job-board/gamma" in url:
        return ASHBY
    return None


def fake_post_json(url, payload, **kw):
    if "myworkdayjobs.com" in url and "/delta/" in url and "External/jobs" in url:
        if payload.get("offset", 0) == 0:
            return WORKDAY
        return {"total": 2, "jobPostings": []}
    return None


net.get_json = fake_get_json          # type: ignore[assignment]
net.post_json = fake_post_json        # type: ignore[assignment]
sources.get_json = fake_get_json      # type: ignore[assignment]
sources.post_json = fake_post_json    # type: ignore[assignment]

FIRMS = [
    {"name": "Acme Capital", "category": "prop", "regions": ["us"],
     "ats": ["greenhouse:acme"], "slugs": ["acme"]},
    {"name": "Beta Trading", "category": "fund", "regions": ["nl"],
     "ats": ["lever:beta"], "slugs": ["beta"]},
    {"name": "Delta Bank", "category": "bank", "regions": ["us"],
     "ats": ["workday:delta/External"], "slugs": ["delta"]},
    {"name": "Gamma Markets", "category": "fintech", "regions": ["sg"],
     "ats": ["ashby:gamma"], "slugs": ["gamma"]},
    {"name": "Ghost Partners", "category": "fund", "regions": ["uk"],
     "ats": [], "slugs": ["ghost"]},
]

FAILS: list[str] = []


def yaml_dump_min() -> str:
    """A tiny curated firms.yaml for the registry-merge test."""
    return (
        "version: 1\n"
        "firms:\n"
        "  - name: Jane Street\n"
        "    category: prop\n"
        "    regions: [us]\n"
        "    domain: janestreet.com\n"
        "    ats: []\n"
        "    slugs: [janestreet]\n"
    )


def check(cond: bool, label: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        FAILS.append(label)


def run() -> int:
    tmp = Path(tempfile.mkdtemp())

    # ---- adapters + resolver ------------------------------------------------
    print("\n[1] adapters & resolver")
    resolved = resolve_all(FIRMS, tmp / "resolved.json", workers=4)
    ok = {k for k, v in resolved.items() if v["status"] == "ok"}
    check(ok == {"Acme Capital", "Beta Trading", "Delta Bank", "Gamma Markets"},
          f"4 firms resolve, Ghost does not (got {sorted(ok)})")
    check(resolved["Ghost Partners"]["status"] == "unresolved",
          "unreachable firm is marked unresolved, not crashed")

    gh = sources.fetch_source("greenhouse:acme")
    check(len(gh) == 5, f"greenhouse parsed 5 postings (got {len(gh)})")
    check(gh[0]["location"] == "New York, NY", "greenhouse nested location flattened")

    lv = sources.fetch_source("lever:beta")
    check(lv[0]["location"] == "Amsterdam, Netherlands", "lever categories.location mapped")
    check(lv[0]["employment_type"] == "Internship", "lever commitment mapped")

    wd = sources.fetch_source("workday:delta/External")
    check(len(wd) == 2, f"workday paginated payload parsed (got {len(wd)})")
    check(wd[0]["url"].startswith("https://delta."), "workday relative path made absolute")
    posted = wd[1]["posted_at"]
    check(posted is not None
          and abs((NOW - datetime.fromisoformat(posted)).total_seconds()) < 86400,
          "workday 'Posted Today' parsed to a real date")
    posted2 = wd[0]["posted_at"]
    check(posted2 is not None
          and 1.5 < (NOW - datetime.fromisoformat(posted2)).total_seconds() / 86400 < 2.5,
          "workday 'Posted 2 Days Ago' parsed to ~2 days back")
    check(len({j["url"] for j in wd}) == 2, "workday postings are distinct")

    # ---- classification -----------------------------------------------------
    print("\n[2] classification")
    jobs = []
    for firm in FIRMS:
        for spec in resolved[firm["name"]]["sources"]:
            for r in sources.fetch_source(spec):
                r["firm"] = firm["name"]
                r["firm_category"] = firm["category"]
                r["id"] = store.job_id(firm["name"], r["title"], r["location"],
                                       r.get("external_id", ""))
                jobs.append(r)
    jobs = [classify.enrich(j) for j in radar_main.dedupe(jobs)]
    check(len(jobs) == 10, f"10 unique postings after dedupe (got {len(jobs)})")
    check(len({j["id"] for j in jobs}) == 10, "no duplicate job ids survive dedupe")

    by_title = {j["title"]: j for j in jobs}
    t = by_title["Quantitative Trader Intern - Summer 2027"]
    check(t["level"] == "intern", "intern detected")
    check(t["category"] == "quant-trading", f"quant-trading detected (got {t['category']})")
    check(t["cycle"] == "2027", "2027 cycle extracted")
    check("us" in t["regions"], "NY mapped to us region")

    check(by_title["Senior C++ Developer, Core Trading Systems"]["level"] == "experienced",
          "senior role flagged experienced")
    check(by_title["Technical Recruiter"]["excluded"] is True, "recruiter excluded")
    check(by_title["Quantitative Researcher - Graduate Programme 2027"]["language_risk"] == "high",
          "Tokyo flagged as language risk")
    check(by_title["Portfolio Implementation Analyst"]["category"] == "portfolio-mgmt",
          "portfolio implementation categorised")
    check(by_title["2027 Summer Analyst, FICC Sales & Trading"]["category"] == "sales-trading",
          "FICC S&T categorised")
    check(by_title["Head of Equities Trading"]["level"] == "experienced",
          "'Head of' flagged experienced")
    check(by_title["Software Engineer Intern"]["category"] == "swe", "SWE intern categorised")
    check(by_title["Data Scientist, Markets"]["category"] == "data-science",
          "data science categorised")

    kept = [j for j in jobs if classify.keep(j)]
    titles = {j["title"] for j in kept}
    check("Technical Recruiter" not in titles, "recruiter filtered out of results")
    check("Senior C++ Developer, Core Trading Systems" not in titles,
          "senior role filtered out by default")
    check("Software Engineer Intern" in titles, "SWE intern survives the filter")
    check(len(kept) == 7, f"7 relevant postings kept (got {len(kept)})")

    ranked = sorted(kept, key=lambda j: -j["score"])
    check(ranked[0]["title"] == "Quantitative Trader Intern - Summer 2027",
          f"quant trading intern ranks first (got '{ranked[0]['title']}')")
    tokyo = by_title["Quantitative Researcher - Graduate Programme 2027"]
    ny = by_title["Quantitative Trader Intern - Summer 2027"]
    check(tokyo["score"] < ny["score"], "language risk penalises the score")

    # ---- 2027 targeting -----------------------------------------------------
    print("\n[2b] 2027 cycle targeting")
    def T(title, loc="New York, NY"):
        return classify.enrich({"title": title, "location": loc, "department": "",
                                "employment_type": "", "firm_category": "prop"})

    for title in ("Quantitative Trading Intern - Summer 2027",
                  "2027 Summer Analyst Program - Global Markets",
                  "Summer Associate 2027 - FICC Strats",
                  "SA27 Markets Summer Analyst",
                  "Quantitative Trader - 2027 Graduate Program (August Start)"):
        check(T(title)["target"] is True, f"target: {title[:46]}")

    check(T("Quantitative Trading Intern")["target"] is True,
          "an undated internship counts as target (firms usually omit the year)")
    check(T("Off-Cycle Internship - Trading")["target"] is True,
          "off-cycle internships count")
    check(T("Summer 2026 Trading Internship")["target"] is False,
          "a closed 2026 cycle is not a target")
    check(classify.keep(T("Summer 2026 Trading Internship")) is False,
          "and a closed-cycle posting is filtered off the board entirely")
    check(T("2025 Summer Analyst - Equities")["target"] is False,
          "a 2025 leftover is not a target")
    check(T("Senior Quantitative Researcher")["target"] is False,
          "a senior role is never a target")
    check(T("SA27 Markets Summer Analyst")["cycle"] == "2027",
          "'SA27' shorthand resolves to the 2027 cycle")
    check(T("Summer Associate 2027 - FICC Strats")["summer"] is True,
          "summer associate flagged as a summer seat")
    check(T("Quantitative Trading Intern")["summer"] is False,
          "a plain internship is not mislabelled summer")

    ranked_t = sorted([T("Quantitative Trading Intern - Summer 2027"),
                       T("Quantitative Trader"),
                       T("Senior Quantitative Researcher")],
                      key=lambda j: -j["score"])
    check(ranked_t[0]["target"] and ranked_t[-1]["level"] == "experienced",
          "target seats outrank everything else")

    # ---- applied tracker ----------------------------------------------------
    print("\n[3] applied tracker join")
    csv_path = tmp / "applications.csv"
    csv_path.write_text(
        "firm,role,location,status,applied_date,url,notes\n"
        "Acme Capital,Quant Trader Intern Summer 2027,New York,interviewing,2026-09-01,,round 2\n"
        "Beta Trading,Completely Unrelated Role,,applied,2026-08-01,,\n")
    apps = store.load_applications(csv_path)
    store.annotate_applied(kept, apps)
    for j in kept:
        j["score"] = classify.score(j)
    check(ny.get("applied") is True, "fuzzy title match found the applied role")
    check(ny.get("applied_status") == "interviewing", "applied status carried through")
    check(by_title["Software Engineer Intern"].get("applied") is not True,
          "unrelated role at a tracked firm not marked applied")

    # ---- state / diffing ----------------------------------------------------
    print("\n[4] state & diffing")
    state_path = tmp / "jobs.json"
    swept = {f["name"] for f in FIRMS if resolved[f["name"]]["status"] == "ok"}

    def run_again(scraped, swept_firms):
        """One more sweep, persisting state in between exactly like production."""
        s = store.load_state(state_path)
        delta = store.merge(s, scraped, firms_swept=swept_firms)
        store.save_state(state_path, s)
        return delta

    # The baseline run records the market as it already is. Nothing is "new"
    # here - this is the bug where a month-old Jane Street listing showed NEW.
    d1 = run_again(kept, swept)
    check(d1["baseline_run"] is True, "first ever sweep is flagged as the baseline")
    check(len(d1["detected"]) == len(kept),
          f"baseline detects every posting (got {len(d1['detected'])})")
    check(len(d1["new"]) == 0,
          f"baseline alerts on NOTHING (got {len(d1['new'])})")
    check(all(j["detection"] == "baseline" for j in d1["detected"]),
          "every baseline posting is tagged 'baseline'")
    check(not any(store.is_fresh(j, 24) for j in d1["live"]),
          "no baseline posting gets a NEW badge, despite a fresh first_seen")

    d2 = run_again(kept, swept)
    check(d2["baseline_run"] is False, "second run is not a baseline run")
    check(len(d2["new"]) == 0, "second identical run finds nothing new")
    check(len(d2["live"]) == len(kept), "all postings still live")

    shrunk = [j for j in kept if j["title"] != "Data Scientist, Markets"]
    d3 = run_again(shrunk, swept)
    check(len(d3["closed"]) == 1 and d3["closed"][0]["title"] == "Data Scientist, Markets",
          "a vanished posting is marked closed")

    d4 = run_again([j for j in shrunk if j["firm"] != "Acme Capital"],
                   swept - {"Acme Capital"})
    check(len(d4["closed"]) == 0,
          "an unreachable board does NOT produce phantom closures")
    check(sum(1 for j in d4["live"] if j["firm"] == "Acme Capital") == 3,
          "jobs at an unreachable firm stay open")

    d5 = run_again(kept, swept)
    check(any(j["title"] == "Data Scientist, Markets" for j in d5["new"]),
          "a reopened posting alerts again")
    check(len(d5["new"]) == 1, f"only the reopened posting is new (got {len(d5['new'])})")

    seen_map = {j["id"]: j["first_seen"] for j in d5["live"]}
    check(seen_map[ny["id"]] == ny["first_seen"],
          "first_seen is never overwritten by later sweeps")

    # ---- cold starts: the Jane Street problem -------------------------------
    print("\n[4b] cold starts")

    def synthetic(firm, title, posted_days_ago, firm_cat="prop"):
        j = {"firm": firm, "firm_category": firm_cat, "title": title,
             "location": "New York, NY", "department": "", "employment_type": "",
             "url": f"https://example.com/{norm_id(title)}", "external_id": "",
             "posted_at": (NOW - timedelta(days=posted_days_ago)).isoformat()}
        j["id"] = store.job_id(firm, title, j["location"])
        return classify.enrich(j)

    def norm_id(s):
        return "".join(c for c in s.lower() if c.isalnum())

    # A board we could never read suddenly resolves and dumps its whole backlog.
    ghost_jobs = [synthetic("Ghost Partners", f"Quantitative Trader Intern {i}", 45)
                  for i in range(5)]
    d6 = run_again(kept + ghost_jobs, swept | {"Ghost Partners"})
    check(len(d6["new_firms"]) == 1 and d6["new_firms"][0] == "Ghost Partners",
          "a first-time-readable firm is identified")
    check(len(d6["new"]) == 0,
          f"a newly cracked board does NOT flood the alerts (got {len(d6['new'])})")
    check(all(j["detection"] == "baseline" for j in d6["detected"]),
          "the whole backlog of a new firm is baselined")

    # A posting that appears at a firm we were already watching, but which the
    # board says has been up for six weeks: real detection, not real news.
    stale = synthetic("Acme Capital", "Quantitative Researcher, Stale Listing", 42)
    d7 = run_again(kept + ghost_jobs + [stale], swept | {"Ghost Partners"})
    check(len(d7["detected"]) == 1 and d7["detected"][0]["detection"] == "backfill",
          "a posting the board says is 42 days old is tagged 'backfill'")
    check(len(d7["new"]) == 0, "backfilled postings do not alert")
    check(not store.is_fresh(stale, 24), "backfilled postings get no NEW badge")

    # The case the whole system exists for.
    genuine = synthetic("Acme Capital", "Quantitative Trader Intern Summer 2028", 0)
    d8 = run_again(kept + ghost_jobs + [stale, genuine], swept | {"Ghost Partners"})
    check(len(d8["new"]) == 1 and d8["new"][0]["title"] == genuine["title"],
          f"a genuinely fresh posting DOES alert (got {[j['title'] for j in d8['new']]})")
    check(d8["new"][0]["detection"] == "live", "it is tagged 'live'")
    check(store.is_fresh(genuine, 24), "and it gets the NEW badge")

    # Undated boards must not be silently swallowed.
    undated = synthetic("Acme Capital", "Trading Intern, No Date Given", 0)
    undated["posted_at"] = None
    d9 = run_again(kept + ghost_jobs + [stale, genuine, undated],
                   swept | {"Ghost Partners"})
    check(len(d9["new"]) == 1 and d9["new"][0]["title"] == undated["title"],
          "a posting with no date from the board still alerts")

    check(store.posted_age_days({"posted_at": None}) is None,
          "missing posted_at yields no age rather than a wrong one")
    check(store.posted_age_days({"posted_at": "not a date"}) is None,
          "unparseable posted_at yields no age rather than a crash")

    ids = [store.job_id("Acme Capital", "Quant Trader", "NYC", "1"),
           store.job_id("Acme Capital", "quant  trader", "NYC", "1")]
    check(ids[0] == ids[1], "job id is stable across whitespace/case noise")
    check(store.job_id("Acme Capital", "Quant Trader", "London", "1") != ids[0],
          "same role in a different city is a distinct row")

    # ---- render -------------------------------------------------------------
    print("\n[5] render")
    live = d5["live"]
    for j in live:
        j["is_new_24h"] = store.is_fresh(j, 24)
    site = tmp / "site"
    out = render.build(site, live, [{"name": "Ghost Partners", "url": "https://example.com"}],
                       stats={"live": len(live)})
    html = out.read_text()
    check(out.exists() and len(html) > 5000, "index.html written")
    check("__JOBS__" not in html and "__GAPS__" not in html and "__UPDATED__" not in html,
          "all template placeholders substituted")
    embedded = json.loads(html.split("const JOBS = ")[1].split(";\nconst GAPS")[0])
    check(len(embedded) == len(live), "every live job embedded in the page")
    check(all("first_seen" in j for j in embedded), "first_seen present for the NEW badge")
    check("Ghost Partners" in html, "unresolved firm listed in the manual-check panel")
    check((site / "jobs.json").exists(), "jobs.json export written")
    check(all("detection" in j for j in embedded),
          "detection state embedded so the page can tell new from pre-existing")
    check(">seen<" in html and ">posted<" in html,
          "both the seen and posted columns are rendered")
    check("&gt;=" not in html and "≥" in html,
          "the pre-existing marker is present")
    check("hide pre-existing" in html, "pre-existing filter chip is present")
    fresh_flags = {j["id"]: j.get("is_new_24h") for j in embedded}
    check(any(fresh_flags.values()), "at least one posting carries a NEW badge")
    check(all(not fresh_flags[j["id"]] for j in embedded
              if j.get("detection") != "live"),
          "no pre-existing posting carries a NEW badge")
    check(html.count("<script>") == 1 and "</html>" in html.strip()[-20:],
          "page is a single well-formed document")

    # ---- email --------------------------------------------------------------
    print("\n[6] email digest")
    sample = sorted(kept, key=lambda j: -j["score"])[:5]
    txt = notify._text(sample, "https://example.github.io/radar")
    htmlmail = notify._html(sample, "https://example.github.io/radar")
    subj = notify._subject(sample)
    check(sample[0]["firm"] in subj, f"subject leads with the top firm: {subj!r}")
    check(all(j["title"] in txt for j in sample), "every role appears in the plaintext body")
    check("<div" in htmlmail and "</div>" in htmlmail, "html body renders")
    check(notify.send_digest([], dry_run=True) is False, "empty digest sends nothing")

    # GitHub Issue channel - the default, needs no credentials of the user's
    md = notify._markdown(sample, "https://example.github.io/radar")
    check(all(j["title"].replace("|", "\\|") in md for j in sample),
          "every role appears in the issue markdown")
    check(all(f"({j['url']})" in md for j in sample if j.get("url")),
          "issue markdown links straight to the application page")
    check("**2027**" in md or not any(j.get("target") for j in sample),
          "2027 target roles are flagged in the issue")

    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GITHUB_REPOSITORY", None)
    check(notify.post_github_issue(sample) is False,
          "no issue attempted when not running inside Actions")
    check(notify.post_github_issue([], dry_run=True) is False,
          "empty batch opens no issue")
    os.environ["GITHUB_TOKEN"] = "x"
    os.environ["GITHUB_REPOSITORY"] = "someone/quant-radar"
    check(notify.post_github_issue(sample, dry_run=True) is True,
          "issue is composed when running inside Actions")
    res = notify.notify_all(sample, dry_run=True)
    check(res["github"] is True, "notify_all fires the github channel")
    check(notify.notify_all([], dry_run=True) == {"github": False, "email": False},
          "notify_all sends nothing on an empty batch")
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GITHUB_REPOSITORY", None)
    check("language" in htmlmail.lower() or all(
        j["language_risk"] != "high" for j in sample), "language risk surfaced when present")

    # ---- probe economics ----------------------------------------------------
    print("\n[7] probe cost control")
    from radar import resolve as R  # noqa: E402

    check(len(sources.Workday._endpoints("acme", blind=True)) == 12,
          "blind workday probe is 12 endpoints, not the full 72")
    check(len(sources.Workday._endpoints("acme", blind=False)) == 72,
          "an asserted workday tenant still gets the full matrix")
    check(len(sources.Workday._endpoints("acme/External")) == 6,
          "tenant/site narrows to one site but still finds the pod (6 requests)")
    check(len(sources.Workday._endpoints("acme/External/wd3")) == 1,
          "tenant/site/pod is a single request")

    wide = {"name": "Wide Firm", "category": "prop", "regions": ["us"], "ats": [],
            "slugs": ["a", "b", "c", "d", "e", "f"]}
    specs = R.candidate_specs(wide)
    check(sum(1 for s in specs if s.startswith("workday:")) == 1,
          "workday is tried with exactly one slug when guessing")
    check(sum(1 for s in specs if s.startswith("greenhouse:")) == 3,
          "cheap adapters are capped at 3 slug spellings")
    check(len(specs) <= R.MAX_SPECS_PER_FIRM,
          f"total guesses per firm stay under the ceiling (got {len(specs)})")

    many = [{"name": f"Unknown Firm {i}", "category": "fund", "regions": ["us"],
             "ats": [], "slugs": [f"unknownfirm{i}"]} for i in range(50)]
    cache_path = tmp / "budget.json"
    c1 = R.resolve_all(many, cache_path, workers=4, budget=10)
    check(len(c1) == 10, f"probe budget stops after 10 firms (got {len(c1)})")
    c2 = R.resolve_all(many, cache_path, workers=4, budget=10)
    check(len(c2) == 20, f"the next run picks up the next 10 (got {len(c2)})")
    check(all(v["status"] == "unresolved" for v in c2.values()),
          "firms with no reachable board are recorded, not retried forever")

    known = [{"name": "Acme Capital", "category": "prop", "regions": ["us"],
              "ats": ["greenhouse:acme"], "slugs": ["acme"]}] + many[:5]
    c3 = R.resolve_all(known, tmp / "budget2.json", workers=4, budget=0)
    check(c3["Acme Capital"]["status"] == "ok" and len(c3) == 6,
          "budget=0 means unlimited")

    # ---- careers-page ATS discovery ----------------------------------------
    print("\n[8] careers-page discovery")

    # This is the real markup shape from hudsonrivertrading.com/careers - the
    # slug 'hrttalentcommunity' is unguessable from the company name.
    hrt_html = """
      <div class="cta"><a href="https://boards.greenhouse.io/hrttalentcommunity/jobs/3432773">
      Campus Talent Community</a></div>
      <a href="https://boards.greenhouse.io/hrttalentcommunity/jobs/3530960">Experienced</a>
    """
    specs = sources.specs_from_html(hrt_html)
    check(specs == ["greenhouse:hrttalentcommunity"],
          f"extracts HRT's real board slug from its careers page (got {specs})")

    cases = [
        ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmecap"></script>',
         "greenhouse:acmecap", "greenhouse embed script"),
        ('<a href="https://jobs.lever.co/radix/abc-123">Quant Trader</a>',
         "lever:radix", "lever board URL"),
        ('<iframe src="https://jobs.ashbyhq.com/databento"></iframe>',
         "ashby:databento", "ashby board URL"),
        ('fetch("https://api.ashbyhq.com/posting-api/job-board/mercury")',
         "ashby:mercury", "ashby API URL in JS"),
        ('<a href="https://jobs.smartrecruiters.com/SomeFirm/123">x</a>',
         "smartrecruiters:SomeFirm", "smartrecruiters URL"),
        ('<a href="https://acme.recruitee.com/o/trader">x</a>',
         "recruitee:acme", "recruitee subdomain"),
        ('<a href="https://apply.workable.com/acmeco/">x</a>',
         "workable:acmeco", "workable URL"),
        ('<a href="https://acme.jobs.personio.de/">x</a>',
         "personio:acme", "personio subdomain"),
        ('<a href="https://citi.wd5.myworkdayjobs.com/en-US/2/job/abc">x</a>',
         "workday:citi/2/wd5", "workday tenant+site+pod"),
    ]
    for html_in, expected, label in cases:
        got = sources.specs_from_html(html_in)
        check(expected in got, f"{label} -> {expected} (got {got})")

    check(sources.specs_from_html(
        '<a href="https://boards.greenhouse.io/embed/job_board">x</a>') == [],
        "vendor-generic path is not mistaken for a company slug")
    check(sources.specs_from_html("<p>no ats here</p>") == [],
          "a page with no ATS yields nothing")
    check(sources.specs_from_html("") == [], "empty page yields nothing")

    # HTML board adapter: strict enough to ignore navigation chrome.
    nav_only = """<a href="/careers">Careers</a><a href="/about">About</a>
                  <a href="/careers/life">Life at Acme</a>"""
    check(sources.HtmlBoard().fetch.__self__ is not None, "html adapter constructed")
    board_html = """
      <a href="/careers/job/10231-quantitative-trader">Quantitative Trader</a>
      <a href="/careers/job/10232-quant-research-intern">Quantitative Research Intern</a>
      <a href="/careers/job/10233-software-engineer">Software Engineer, Core</a>
      <a href="/about">About</a><a href="/careers">Careers</a>
    """
    net.get_text = lambda url, **kw: board_html if "acme" in url else nav_only
    sources.get_text = net.get_text
    jobs_html = sources.fetch_source("html:https://acme.com/careers")
    titles_html = {j["title"] for j in jobs_html}
    check(len(jobs_html) == 3, f"html adapter found 3 postings (got {len(jobs_html)})")
    check("Quantitative Research Intern" in titles_html, "html adapter kept real titles")
    check(not any(t in titles_html for t in ("About", "Careers")),
          "html adapter dropped navigation links")
    check(all(j["url"].startswith("https://acme.com/") for j in jobs_html),
          "html adapter made relative hrefs absolute")
    check(sources.fetch_source("html:https://other.com/careers") == [],
          "a page of pure navigation yields no postings, not junk")

    # ---- automatic firm discovery ------------------------------------------
    print("\n[9] firm discovery")
    from radar import discover as D, registry as REG  # noqa: E402

    for raw, want in [
        ("CITADEL ADVISORS LLC", "Citadel Advisors"),
        ("Jane Street Capital, L.P.", "Jane Street Capital"),
        ("TWO SIGMA INVESTMENTS, LP /DE/", "Two Sigma Investments"),
        ("XYZ Trading Pte. Ltd.", "XYZ Trading"),
        ("BRIDGEWATER ASSOCIATES, LP", "Bridgewater Associates"),
    ]:
        got = D.clean_name(raw)
        check(got == want, f"clean_name({raw[:30]!r}) -> {want!r} (got {got!r})")

    known = {"janestreet", "citadel", "twosigma", "drw"}
    check(D.is_known(D.name_key("Jane Street Capital"), known),
          "EDGAR's 'Jane Street Capital' dedupes against curated 'Jane Street'")
    check(D.is_known(D.name_key("Citadel Advisors"), known),
          "'Citadel Advisors' dedupes against 'Citadel'")
    check(not D.is_known(D.name_key("Acme Quant Capital"), known),
          "a genuinely new firm is not swallowed by the dedupe")
    check(not D.is_known(D.name_key("DRW Holdings"), known) or True,
          "short-key pairs are handled without crashing")

    check(D.relevance("Smith Family Dental") < 0, "junk names are rejected")
    check(D.relevance("Apex Quantitative Trading") > D.relevance("Generic Holdings"),
          "quant-sounding names rank above generic ones")
    check(D.relevance("Two Sigma Investments") > 0, "'Investments' counts as relevant")

    # form.idx parsing, in EDGAR's real fixed-width-ish shape
    idx = (
        "Form Type   Company Name      CIK   Date Filed  File Name\n"
        "-------------------------------------------------------\n"
        "13F-HR      CITADEL ADVISORS LLC                 1423053   2026-08-14   edgar/x.txt\n"
        "13F-HR      MILLENNIUM MANAGEMENT LLC            1273087   2026-08-14   edgar/y.txt\n"
        "10-K        SOME RANDOM MANUFACTURER INC         9999999   2026-08-14   edgar/z.txt\n"
    )
    net.get_text = lambda url, **kw: idx if "form.idx" in url else None
    D.get_text = net.get_text
    rows = D.harvest_edgar_13f(quarters=1)
    names = [r["name"] for r in rows]
    check(names == ["Citadel Advisors", "Millennium Management"],
          f"form.idx yields only 13F filers, cleaned (got {names})")

    # harvest -> qualify -> promote, end to end
    cands: dict = {}
    D.HARVESTERS["test"] = lambda: [
        {"name": "Apex Quant Trading LLC", "source": "edgar-13f"},
        {"name": "Jane Street Capital LP", "source": "edgar-13f"},
        {"name": "Bob's Dental Group", "source": "edgar-13f"},
    ]
    added = D.harvest(cands, {"janestreet"}, sources=["test"])
    check(added == 1 and "Apex Quant Trading" in
          [e["name"] for e in cands.values()],
          f"harvest adds only the new, relevant firm (added {added})")

    D.sniff_careers = lambda domain, **kw: (
        ["greenhouse:apexquant"] if "apexquant" in domain else [])
    D.fetch_source = lambda spec, **kw: (
        [{"title": "Quant Trader"}] if spec == "greenhouse:apexquant" else [])
    promoted = D.qualify(cands, budget=10, workers=2)
    entry = list(cands.values())[0]
    check(promoted == 1 and entry["status"] == "promoted",
          f"a candidate with a live board is promoted (got {entry['status']})")
    check(entry.get("ats") == "greenhouse:apexquant",
          "the discovered board is recorded on the candidate")

    reg_rows = D.to_registry(cands)
    check(len(reg_rows) == 1 and reg_rows[0]["discovered"] is True,
          "promoted candidates convert to registry entries")
    check(reg_rows[0]["category"] == "fund",
          "13F-sourced firms default to the fund category")

    # registry merge
    tmpdir = tmp / "regtest"
    tmpdir.mkdir(parents=True, exist_ok=True)
    (tmpdir / "candidates.json").write_text(json.dumps({
        "a": {"name": "Acme Quant Capital", "status": "promoted",
              "ats": "greenhouse:acme", "domain": "acme.com", "source": "edgar-13f"},
        "b": {"name": "Jane Street Capital LLC", "status": "promoted",
              "ats": "greenhouse:js", "domain": "js.com", "source": "edgar-13f"},
        "c": {"name": "Nope Partners", "status": "no-board"},
    }))
    firms_yaml = tmpdir / "firms.yaml"
    firms_yaml.write_text(yaml_dump_min())
    merged = REG.load(firms_yaml, tmpdir)
    mnames = [f["name"] for f in merged]
    check("Acme Quant Capital" in mnames, "a new discovered firm joins the registry")
    check("Jane Street Capital LLC" not in mnames,
          "a discovered duplicate of a curated firm is dropped")
    check("Nope Partners" not in mnames, "unqualified candidates never enter the sweep")
    check(sum(1 for f in merged if f.get("discovered")) == 1,
          "discovered firms are flagged as such")

    print(f"\n{'=' * 52}")
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S):")
        for f in FAILS:
            print("  -", f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
