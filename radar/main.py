"""Entry point. One sweep = resolve -> fetch -> classify -> diff -> render -> notify."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import classify, discover, notify, registry, render, store
from .resolve import DEFAULT_PROBE_BUDGET, resolve_all
from .sources import fetch_source

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SITE = ROOT / "site"

log = logging.getLogger("radar")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def load_firms(path: Path, limit: int | None = None) -> list[dict]:
    return registry.load(path, DATA, limit=limit)


def sweep(firms: list[dict], resolved: dict, *, workers: int) -> tuple[list[dict], set[str]]:
    """Fetch every resolved board. Returns (raw jobs, firms actually swept)."""
    tasks: list[tuple[dict, str]] = []
    for firm in firms:
        for spec in (resolved.get(firm["name"]) or {}).get("sources", []):
            tasks.append((firm, spec))

    jobs: list[dict] = []
    swept: set[str] = set()
    failures = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_source, spec): (firm, spec) for firm, spec in tasks}
        for fut in as_completed(futures):
            firm, spec = futures[fut]
            try:
                raw = fut.result()
            except Exception as exc:
                log.warning("fetch failed %s (%s): %s", firm["name"], spec, exc)
                failures += 1
                continue
            if raw:
                swept.add(firm["name"])
            for r in raw:
                r["firm"] = firm["name"]
                r["firm_category"] = firm["category"]
                r["id"] = store.job_id(firm["name"], r["title"], r["location"],
                                       r.get("external_id", ""))
                jobs.append(r)

    log.info("swept %d boards across %d firms -> %d raw postings (%d failures)",
             len(tasks), len(swept), len(jobs), failures)
    return jobs, swept


def dedupe(jobs: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for j in jobs:
        prev = seen.get(j["id"])
        if prev is None or (not prev.get("url") and j.get("url")):
            seen[j["id"]] = j
    return list(seen.values())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="quant-radar")
    ap.add_argument("--firms", type=Path, default=ROOT / "firms.yaml")
    ap.add_argument("--limit-firms", type=int, default=None,
                    help="only process the first N firms (for testing)")
    ap.add_argument("--workers", type=int, default=14)
    ap.add_argument("--probe-budget", type=int, default=None,
                    help="max not-yet-known firms to discover this run "
                         "(0 = unlimited; default 160)")
    ap.add_argument("--force-resolve", action="store_true",
                    help="re-probe every firm's ATS, ignoring the cache")
    ap.add_argument("--resolve-only", action="store_true")
    ap.add_argument("--discover", action="store_true",
                    help="harvest new candidate firms from SEC EDGAR and "
                         "exchange member directories, then qualify a batch")
    ap.add_argument("--discover-only", action="store_true")
    ap.add_argument("--discover-budget", type=int, default=None,
                    help="candidates to qualify this run (default 120)")
    ap.add_argument("--discover-sources", default=None,
                    help="comma list: edgar-13f,edgar-sic,members")
    ap.add_argument("--include-experienced", action="store_true",
                    help="keep senior roles in the output")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--dry-run-email", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    started = datetime.now(timezone.utc)
    DATA.mkdir(parents=True, exist_ok=True)

    if args.discover or args.discover_only:
        dstats = discover.run(
            DATA, registry.known_keys(args.firms),
            sources=(args.discover_sources.split(",")
                     if args.discover_sources else None),
            budget=(discover.QUALIFY_BUDGET if args.discover_budget is None
                    else args.discover_budget),
            workers=args.workers)
        (DATA / "last_discovery.json").write_text(json.dumps(dstats, indent=1))
        if args.discover_only:
            return 0

    firms = load_firms(args.firms, args.limit_firms)
    log.info("registry: %d firms (%d discovered)",
             len(firms), sum(1 for f in firms if f.get("discovered")))

    budget = (DEFAULT_PROBE_BUDGET if args.probe_budget is None
              else args.probe_budget)
    resolved = resolve_all(firms, DATA / "resolved.json",
                           workers=args.workers, force=args.force_resolve,
                           budget=budget)
    if args.resolve_only:
        return 0

    raw, swept = sweep(firms, resolved, workers=args.workers)
    raw = dedupe(raw)

    kept = []
    for j in raw:
        classify.enrich(j)
        if classify.keep(j, include_experienced=args.include_experienced):
            kept.append(j)
    log.info("relevance filter: %d kept / %d raw", len(kept), len(raw))

    apps = store.load_applications(ROOT / "applications.csv")
    store.annotate_applied(kept, apps)
    for j in kept:
        j["score"] = classify.score(j)

    state = store.load_state(DATA / "jobs.json")
    delta = store.merge(state, kept, firms_swept=swept)

    live = delta["live"]
    for j in live:
        j["is_new_24h"] = store.is_fresh(j, 24)

    gaps = [{"name": f["name"], "url": render.careers_guess(f)}
            for f in firms
            if (resolved.get(f["name"]) or {}).get("status") != "ok"]

    detected = delta["detected"]
    stats = {
        "firms_total": len(firms),
        "firms_resolved": len(firms) - len(gaps),
        "firms_swept": len(swept),
        "raw_postings": len(raw),
        "relevant": len(kept),
        "live": len(live),
        "target_live": sum(1 for j in live if j.get("target")),
        "detected_this_run": len(detected),
        "new_this_run": len(delta["new"]),
        "baselined_this_run": sum(1 for j in detected if j.get("detection") == "baseline"),
        "backfilled_this_run": sum(1 for j in detected if j.get("detection") == "backfill"),
        "closed_this_run": len(delta["closed"]),
        "baseline_run": delta["baseline_run"],
        "firms_first_read": delta["new_firms"][:40],
        "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }

    render.build(SITE, live, gaps, stats=stats)
    store.save_state(DATA / "jobs.json", state)
    (DATA / "last_run.json").write_text(json.dumps(stats, indent=1))

    # delta["new"] already excludes baselined and backfilled postings. On top of
    # that: every 2027 intern / summer analyst / summer associate seat alerts, no
    # matter how it scored. Everything else has to clear the score bar.
    alertable = [j for j in delta["new"]
                 if not j.get("applied")
                 and (j.get("target") or j.get("score", 0) >= 40)]
    if delta["baseline_run"]:
        log.info("baseline run complete - %d postings recorded, no alerts sent. "
                 "The next sweep is the first one that can report anything as new.",
                 len(detected))
    elif not args.no_email:
        notify.notify_all(alertable, dry_run=args.dry_run_email)

    log.info("done in %ss | live=%d detected=%d new=%d (baselined=%d backfilled=%d) "
             "closed=%d alerted=%d",
             stats["duration_s"], stats["live"], stats["detected_this_run"],
             stats["new_this_run"], stats["baselined_this_run"],
             stats["backfilled_this_run"], stats["closed_this_run"],
             0 if delta["baseline_run"] else len(alertable))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
