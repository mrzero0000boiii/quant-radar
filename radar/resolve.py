"""
Discovers which ATS each firm actually uses.

I can't verify 400 boards by hand, so the registry ships *candidates* and this
module probes them. Confirmed sources are cached in data/resolved.json and
re-checked on a TTL, so a steady-state run costs one request per live board.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources import PROBE_ORDER, fetch_source, sniff_careers

log = logging.getLogger("radar.resolve")

RESOLVED_TTL_DAYS = 30      # re-verify a working board monthly
UNRESOLVED_TTL_DAYS = 7     # retry a firm we couldn't crack weekly
MAX_SPECS_PER_FIRM = 26     # hard ceiling on guesses for one firm
# Discovery is the expensive half. Rather than risk a 50-minute Actions job,
# each run probes at most this many not-yet-known firms and leaves the rest for
# the next sweep. Coverage fills in over a day or so instead of all at once.
DEFAULT_PROBE_BUDGET = 160


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() / 86400
    except ValueError:
        return 1e9


def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("resolved.json was corrupt; starting fresh")
    return {}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True))


# How many slug spellings to try per adapter when guessing. The registry puts
# its best guess first, so the tail has a poor hit rate and costs real time.
# Workday is capped hardest because each attempt is a dozen POSTs, not one GET.
SLUGS_PER_ADAPTER = {"workday": 1, "personio": 2}
DEFAULT_SLUGS_PER_ADAPTER = 3


def candidate_specs(firm: dict) -> list[str]:
    """Hints first, then a deliberately bounded adapter x slug matrix."""
    specs: list[str] = []
    for hint in firm.get("ats") or []:
        if hint not in specs:
            specs.append(hint)
    slugs = firm.get("slugs") or []
    for kind in PROBE_ORDER:
        cap = SLUGS_PER_ADAPTER.get(kind, DEFAULT_SLUGS_PER_ADAPTER)
        for slug in slugs[:cap]:
            spec = f"{kind}:{slug}"
            if spec not in specs:
                specs.append(spec)
    return specs


def resolve_firm(firm: dict, cached: dict | None, *, force: bool = False) -> dict:
    """
    Returns a cache entry:
      {sources: [spec, ...], checked_at: iso, status: ok|unresolved, tried: int}
    """
    name = firm["name"]
    if cached and not force:
        status = cached.get("status")
        age = _age_days(cached.get("checked_at"))
        if status == "ok" and age < RESOLVED_TTL_DAYS:
            return cached
        if status == "unresolved" and age < UNRESOLVED_TTL_DAYS:
            return cached

    found: list[str] = []
    tried = 0

    # Phase 1 - things we already know work, plus the registry's explicit hints.
    # A firm can legitimately run two boards (e.g. a US and a EU one), so we try
    # all of these rather than stopping at the first hit.
    known = list((cached or {}).get("sources") or [])
    priority = known + [h for h in (firm.get("ats") or []) if h not in known]
    for spec in priority:
        tried += 1
        if fetch_source(spec):
            found.append(spec)
            log.info("resolved %-38s -> %-22s", name, spec)

    # Phase 2 - read the firm's own careers page and take the ATS URL it
    # publishes. This beats guessing outright: Hudson River Trading's board is
    # "hrttalentcommunity", which no derivation from the company name produces,
    # but it is sitting in an href on hudsonrivertrading.com/careers.
    method = "hint" if found else None
    if not found and firm.get("domain"):
        tried += 1
        for spec in sniff_careers(firm["domain"]):
            if fetch_source(spec):
                found.append(spec)
                method = "careers-page"
                log.info("sniffed  %-38s -> %-22s (via %s)",
                         name, spec, firm["domain"])
                break

    # Phase 3 - blind probe of the adapter x slug matrix. Last resort, and we
    # stop at the first hit so the same board isn't registered twice under two
    # spellings.
    if not found:
        for spec in candidate_specs(firm):
            if spec in priority:
                continue
            tried += 1
            if fetch_source(spec, blind=True):
                found.append(spec)
                method = "slug-guess"
                log.info("guessed  %-38s -> %-22s", name, spec)
                break
            if tried >= MAX_SPECS_PER_FIRM:
                break

    if found:
        return {"sources": found, "checked_at": _now().isoformat(),
                "status": "ok", "tried": tried, "method": method}
    return {"sources": [], "checked_at": _now().isoformat(),
            "status": "unresolved", "tried": tried,
            "note": (cached or {}).get("note", "")}


def resolve_all(firms: list[dict], cache_path: Path, *,
                workers: int = 10, force: bool = False,
                budget: int = DEFAULT_PROBE_BUDGET) -> dict:
    cache = load_cache(cache_path)
    todo = []
    for firm in firms:
        entry = cache.get(firm["name"])
        age = _age_days((entry or {}).get("checked_at"))
        ttl = RESOLVED_TTL_DAYS if (entry or {}).get("status") == "ok" else UNRESOLVED_TTL_DAYS
        if force or entry is None or age >= ttl:
            todo.append(firm)

    # Re-verifying a known board is one request; discovering an unknown one is
    # dozens. Known boards always go first, then discovery fills the budget.
    cheap = [f for f in todo if (cache.get(f["name"]) or {}).get("status") == "ok"]
    expensive = [f for f in todo if (cache.get(f["name"]) or {}).get("status") != "ok"]
    # Never-seen firms before previously-failed ones, so a freshly added firm
    # gets looked at on the very next run.
    expensive.sort(key=lambda f: (f["name"] in cache, _age_days(
        (cache.get(f["name"]) or {}).get("checked_at")) * -1))

    deferred = 0
    if budget and len(expensive) > budget:
        deferred = len(expensive) - budget
        expensive = expensive[:budget]
    todo = cheap + expensive

    if todo:
        log.info("resolving %d/%d firms (%d cached and fresh, %d re-verify, "
                 "%d discovery%s)", len(todo), len(firms), len(firms) - len(todo) - deferred,
                 len(cheap), len(expensive),
                 f", {deferred} deferred to the next run" if deferred else "")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_firm, f, cache.get(f["name"]), force=force): f
                   for f in todo}
        for fut in as_completed(futures):
            firm = futures[fut]
            try:
                cache[firm["name"]] = fut.result()
            except Exception as exc:
                log.warning("resolve failed for %s: %s", firm["name"], exc)
                cache[firm["name"]] = {"sources": [], "status": "unresolved",
                                       "checked_at": _now().isoformat(),
                                       "note": f"error: {exc}"}
    save_cache(cache_path, cache)

    ok = sum(1 for v in cache.values() if v.get("status") == "ok")
    pending = len(firms) - len(cache)
    by_method: dict[str, int] = {}
    for v in cache.values():
        if v.get("status") == "ok":
            by_method[v.get("method") or "hint"] = by_method.get(
                v.get("method") or "hint", 0) + 1
    log.info("registry coverage: %d/%d firms have a live board%s | %s",
             ok, len(firms),
             f" ({pending} not probed yet)" if pending > 0 else "",
             ", ".join(f"{k}={n}" for k, n in sorted(by_method.items())) or "-")
    return cache
