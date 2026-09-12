"""
State: stable job identity, first_seen tracking, and the applied-tracker join.

The point of this file is the `first_seen` timestamp. Careers APIs lie about
posting dates (Workday says "Posted 30+ Days Ago", Greenhouse gives you an
`updated_at` that moves when someone fixes a typo). The only reliable "this is
new" signal is: we swept 4 hours ago and it wasn't there.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger("radar.store")

ARCHIVE_AFTER_DAYS = 21    # keep a closed posting visible this long, then drop it
BACKFILL_AFTER_DAYS = 21   # if the board says it's older than this, it isn't news


def _now() -> datetime:
    return datetime.now(timezone.utc)


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def job_id(firm: str, title: str, location: str, external_id: str = "") -> str:
    """
    Identity survives a URL change or a re-post. It deliberately includes
    location so 'Trading Intern - NYC' and '- London' stay separate rows.
    """
    key = f"{norm(firm)}|{norm(title)}|{norm(location)[:60]}|{external_id or ''}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Applied tracker
# ---------------------------------------------------------------------------

APPLIED_HEADERS = ("firm", "role", "location", "status", "applied_date", "url", "notes")


def load_applications(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            clean = {(k or "").strip().lower(): (v or "").strip()
                     for k, v in row.items() if k}
            if clean.get("firm"):
                rows.append(clean)
    log.info("loaded %d rows from applications tracker", len(rows))
    return rows


def _title_match(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / max(1, len(ta | tb))
    return max(jaccard, SequenceMatcher(None, na, nb).ratio())


def annotate_applied(jobs: list[dict], applications: list[dict]) -> None:
    """Mark jobs the user has already applied to. Firm must match, title fuzzily."""
    by_firm: dict[str, list[dict]] = {}
    for app in applications:
        by_firm.setdefault(norm(app["firm"]), []).append(app)

    for job in jobs:
        firm_key = norm(job["firm"])
        cands = by_firm.get(firm_key)
        if not cands:  # tolerate 'Citadel' vs 'Citadel LLC'
            for k, v in by_firm.items():
                if k and (k in firm_key or firm_key in k) and abs(len(k) - len(firm_key)) < 12:
                    cands = v
                    break
        if not cands:
            continue
        best, best_app = 0.0, None
        for app in cands:
            r = _title_match(job["title"], app.get("role", ""))
            if app.get("url") and job.get("url") and app["url"].strip() == job["url"].strip():
                r = 1.0
            if r > best:
                best, best_app = r, app
        if best >= 0.55 and best_app is not None:
            job["applied"] = True
            job["applied_status"] = best_app.get("status") or "applied"
            job["applied_date"] = best_app.get("applied_date", "")
            job["applied_confidence"] = round(best, 2)


# ---------------------------------------------------------------------------
# Snapshot merge
# ---------------------------------------------------------------------------

def _empty_state() -> dict:
    return {"jobs": {}, "runs": [], "meta": {"baseline_at": None, "firms": {}}}


def load_state(path: Path) -> dict:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("jobs.json corrupt; starting fresh (first_seen history lost)")
        return _empty_state()
    data.setdefault("jobs", {})
    data.setdefault("runs", [])
    data.setdefault("meta", {"baseline_at": None, "firms": {}})
    data["meta"].setdefault("baseline_at", None)
    data["meta"].setdefault("firms", {})
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True))


def posted_age_days(job: dict) -> float | None:
    """How old the BOARD says the posting is. None when the board didn't say."""
    raw = job.get("posted_at")
    if not raw:
        return None
    try:
        return (_now() - datetime.fromisoformat(raw)).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def classify_detection(job: dict, *, baseline_run: bool, firm_is_new: bool) -> str:
    """
    Why did this posting appear in our state just now?

      baseline - our first ever sweep, or our first successful read of this
                 firm's board. It was already sitting there; we just hadn't
                 looked. Never 'new'.
      backfill - we genuinely detected it this run, but the board says it has
                 been up longer than BACKFILL_AFTER_DAYS. Worth showing, not
                 worth waking you up for - you are not going to be first.
      live     - detected this run and either freshly posted or undated.
                 This is the only state that earns a NEW badge or an email.
    """
    if baseline_run or firm_is_new:
        return "baseline"
    age = posted_age_days(job)
    if age is not None and age > BACKFILL_AFTER_DAYS:
        return "backfill"
    return "live"


def merge(state: dict, scraped: list[dict], *, firms_swept: set[str]) -> dict:
    """
    Fold this sweep into the stored state.

    Returns {new, detected, closed, live}:
      detected - everything that entered our state this run, any detection state
      new      - the subset with detection == 'live'; these are alert-worthy

    A job only counts as closed if we actually swept its firm's board this run,
    so a transient API failure never produces a fake 'closed' event.
    """
    now = _now().isoformat()
    stored: dict = state["jobs"]
    meta: dict = state["meta"]

    baseline_run = meta.get("baseline_at") is None
    if baseline_run:
        meta["baseline_at"] = now
        log.info("BASELINE RUN: recording the existing market, alerting on nothing")

    firm_meta: dict = meta["firms"]
    first_time_firms = {f for f in firms_swept if f not in firm_meta}
    if first_time_firms and not baseline_run:
        log.info("%d firm(s) read for the first time - their postings are baselined, "
                 "not alerted: %s", len(first_time_firms),
                 ", ".join(sorted(first_time_firms)[:8]))
    for f in firms_swept:
        firm_meta.setdefault(f, {"first_swept_at": now})

    seen_ids = set()
    detected: list[dict] = []

    for job in scraped:
        jid = job["id"]
        seen_ids.add(jid)
        prev = stored.get(jid)
        if prev is None:
            job["first_seen"] = now
            job["last_seen"] = now
            job["status"] = "open"
            job["detection"] = classify_detection(
                job, baseline_run=baseline_run,
                firm_is_new=job.get("firm") in first_time_firms)
            stored[jid] = job
            detected.append(job)
        else:
            first_seen = prev.get("first_seen", now)
            detection = prev.get("detection", "live")
            reopened = prev.get("status") == "closed"
            prev.update(job)
            prev["first_seen"] = first_seen
            prev["last_seen"] = now
            prev["status"] = "open"
            prev["detection"] = detection
            if reopened:
                # A repost is a real opportunity, but only if the board also
                # says it is fresh - otherwise it's the same stale listing.
                prev["reopened_at"] = now
                prev["detection"] = classify_detection(
                    prev, baseline_run=baseline_run,
                    firm_is_new=prev.get("firm") in first_time_firms)
                detected.append(prev)

    new_jobs = [j for j in detected if j.get("detection") == "live"]

    closed: list[dict] = []
    cutoff = _now() - timedelta(days=ARCHIVE_AFTER_DAYS)
    for jid, job in list(stored.items()):
        if jid in seen_ids:
            continue
        if job.get("firm") not in firms_swept:
            continue  # board wasn't reachable this run - say nothing
        if job.get("status") == "open":
            job["status"] = "closed"
            job["closed_at"] = now
            closed.append(job)
        try:
            closed_at = datetime.fromisoformat(job.get("closed_at", now))
        except ValueError:
            closed_at = _now()
        if closed_at < cutoff:
            del stored[jid]

    live = [j for j in stored.values() if j.get("status") == "open"]
    state["runs"] = ([{"at": now, "scraped": len(scraped), "detected": len(detected),
                       "new": len(new_jobs), "closed": len(closed),
                       "firms": len(firms_swept), "baseline": baseline_run}]
                     + state["runs"])[:120]
    return {"new": new_jobs, "detected": detected, "closed": closed, "live": live,
            "baseline_run": baseline_run, "new_firms": sorted(first_time_firms)}


def is_fresh(job: dict, hours: int) -> bool:
    """
    True only for postings radar itself caught appearing. A baselined or
    backfilled posting is never 'fresh' no matter how recent its first_seen is,
    because first_seen only records when we started looking.
    """
    if job.get("detection", "live") != "live":
        return False
    try:
        seen = datetime.fromisoformat(job.get("first_seen", ""))
    except (ValueError, TypeError):
        return False
    return (_now() - seen) <= timedelta(hours=hours)
