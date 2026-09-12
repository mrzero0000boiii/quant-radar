"""
Automatic firm discovery.

The hand-written registry will never be complete, so this module goes and finds
firms instead. It works in two stages:

  1. HARVEST  - pull candidate firm names from public registries that already
                enumerate the industry: SEC EDGAR 13F filers (every institutional
                manager over $100M), EDGAR's SIC-code directory for investment
                advisers and broker-dealers, and exchange member directories
                (where the prop shops live).
  2. QUALIFY  - for each candidate, find a domain, read its careers page, and see
                whether a real job board comes back. Candidates with a board get
                promoted into the registry and swept like any other firm.

Qualification costs real requests, so it runs on a budget and works through the
backlog over weeks. Candidates are ranked so the quant-sounding names go first.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .net import get_text
from .sources import fetch_source, sniff_careers

log = logging.getLogger("radar.discover")

# SEC requires a real contact string. Overridden by the SEC_USER_AGENT env var.
SEC_UA = "quant-radar/1.0 (job tracker; contact: radar@example.com)"

QUALIFY_BUDGET = 120          # candidates to qualify per discovery run
MAX_CANDIDATES = 40_000


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

SUFFIXES = re.compile(
    r"\b(l\.?l\.?c\.?|l\.?p\.?|l\.?l\.?p\.?|inc\.?|incorporated|corp\.?|"
    r"corporation|ltd\.?|limited|plc|co\.?|company|holdings?|group|"
    r"gmbh|ag|s\.?a\.?|n\.?v\.?|b\.?v\.?|pte\.?|pty\.?|sarl|ab|as|oy|"
    r"trust|the)\b", re.I)
EDGAR_NOISE = re.compile(r"/[A-Z]{2}/?$|/ADV$|\s+/[A-Z]{2}/\s*$")


TRAILING_SUFFIX = re.compile(
    r"[\s,]+(l\.?l\.?c\.?|l\.?p\.?|l\.?l\.?p\.?|inc\.?|corp\.?|ltd\.?|plc|"
    r"gmbh|ag|s\.?a\.?|n\.?v\.?|b\.?v\.?|pte\.?\s*ltd\.?|pte\.?|pty\.?|"
    r"limited|incorporated|corporation|company)\.?$", re.I)


def clean_name(raw: str) -> str:
    n = EDGAR_NOISE.sub("", (raw or "").strip())
    n = re.sub(r"\s+", " ", n).strip()
    if n.isupper() and len(n) > 4:
        n = n.title()
    # EDGAR names end in legal suffixes; strip them repeatedly ("Foo Capital,
    # L.P." and "FOO CAPITAL LLC" should both display as "Foo Capital").
    for _ in range(3):
        stripped = TRAILING_SUFFIX.sub("", n).strip()
        if stripped == n or len(stripped) < 3:
            break
        n = stripped
    return re.sub(r"[,\.]+$", "", n).strip()


def name_key(name: str) -> str:
    """Identity for dedupe: suffixes and punctuation removed."""
    n = SUFFIXES.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


def is_known(key: str, known: set[str]) -> bool:
    """
    Dedupe that survives the naming EDGAR uses. The curated registry says
    "Jane Street"; EDGAR says "JANE STREET CAPITAL, L.P.". Exact key matching
    treats those as two firms and the board gets swept twice under two names,
    so a long key that extends a known one counts as the same firm.
    """
    if not key:
        return True
    if key in known:
        return True
    for k in known:
        if len(k) < 7 or len(key) < 7:
            continue
        if key.startswith(k) or k.startswith(key):
            return True
    return False


def slug_for(name: str) -> str:
    n = SUFFIXES.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "", n)


def domain_guesses(name: str) -> list[str]:
    slug = slug_for(name)
    if not slug or len(slug) < 3:
        return []
    out = [f"{slug}.com"]
    hyph = re.sub(r"[^a-z0-9]+", "-", SUFFIXES.sub(" ", name.lower())).strip("-")
    hyph = re.sub(r"-+", "-", hyph)
    if hyph and hyph != slug:
        out.append(f"{hyph}.com")
    return out[:2]


# ---------------------------------------------------------------------------
# Relevance scoring for candidate names
# ---------------------------------------------------------------------------

STRONG = re.compile(
    r"\b(quant|trading|trader|securities|capital markets|market making|"
    r"market maker|arbitrage|systematic|derivatives|futures|options|"
    r"proprietary|alpha|algorithmic|execution|hedge fund|macro)\b", re.I)
MEDIUM = re.compile(
    r"\b(capital|partners|associates|asset management|investments?|advisors|"
    r"advisers|management|fund|funds|global|research|technologies|holdings|"
    r"brokerage|clearing|financial|wealth|strategies|ventures)\b", re.I)
JUNK = re.compile(
    r"\b(church|ministr|realty|real estate|dental|medical|hospital|clinic|"
    r"insurance agency|automotive|restaurant|plumbing|roofing|landscap|"
    r"municipal|county|school district|university endowment|credit union|"
    r"funeral|salon|trucking|farm|ranch|winery|brewery)\b", re.I)


def relevance(name: str) -> int:
    if JUNK.search(name):
        return -1
    s = 0
    if STRONG.search(name):
        s += 10
    if MEDIUM.search(name):
        s += 3
    if len(name) < 4 or len(name) > 70:
        s -= 2
    return s


# ---------------------------------------------------------------------------
# Harvest sources
# ---------------------------------------------------------------------------

def _sec_headers() -> dict:
    import os
    return {"User-Agent": os.environ.get("SEC_USER_AGENT", SEC_UA),
            "Accept-Encoding": "gzip, deflate"}


def harvest_edgar_13f(quarters: int = 4) -> list[dict]:
    """
    Every institutional investment manager that files a 13F - i.e. everyone
    running over $100M. This is the single richest source of hedge fund and
    asset manager names there is, and it is a plain text index.

    form.idx is fixed-width-ish, pipe-free, with lines like:
        13F-HR  CITADEL ADVISORS LLC  1423053  2026-08-14  edgar/data/...
    """
    out: list[dict] = []
    now = datetime.now(timezone.utc)
    year, q = now.year, (now.month - 1) // 3 + 1
    for _ in range(quarters):
        url = (f"https://www.sec.gov/Archives/edgar/full-index/"
               f"{year}/QTR{q}/form.idx")
        text = get_text(url, headers=_sec_headers())
        if text:
            found = 0
            for line in text.splitlines():
                if not line.startswith("13F-HR"):
                    continue
                rest = line[len("13F-HR"):].strip()
                # company name runs up to the CIK column
                m = re.match(r"(.+?)\s{2,}(\d{4,10})\s+(\d{4}-\d{2}-\d{2})", rest)
                if not m:
                    continue
                out.append({"name": clean_name(m.group(1)),
                            "source": "edgar-13f", "cik": m.group(2)})
                found += 1
            log.info("edgar 13F %sQ%s -> %d filers", year, q, found)
        else:
            log.warning("edgar form.idx unavailable for %sQ%s", year, q)
        q -= 1
        if q == 0:
            year, q = year - 1, 4
    return out


SIC_CODES = {
    "6282": "Investment Advice",
    "6211": "Security Brokers, Dealers & Flotation",
    "6221": "Commodity Contracts Brokers & Dealers",
    "6199": "Finance Services",
    "6726": "Investment Offices",
}


def harvest_edgar_sic(max_pages_per_code: int = 8) -> list[dict]:
    """
    EDGAR's company directory filtered to the finance SIC codes. Catches
    broker-dealers and prop firms that never file a 13F.
    """
    out: list[dict] = []
    row = re.compile(
        r'<td[^>]*>\s*<a[^>]*CIK=(\d+)[^>]*>[^<]*</a>\s*</td>\s*'
        r'<td[^>]*>(.*?)</td>', re.I | re.S)
    for sic in SIC_CODES:
        for page in range(max_pages_per_code):
            url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                   f"&SIC={sic}&type=&dateb=&owner=include&count=100"
                   f"&start={page * 100}&action=getcompany")
            html = get_text(url, headers=_sec_headers())
            if not html:
                break
            matches = row.findall(html)
            if not matches:
                break
            for cik, name in matches:
                name = clean_name(re.sub(r"<[^>]+>", "", name))
                if name:
                    out.append({"name": name, "source": f"edgar-sic-{sic}",
                                "cik": cik})
            if len(matches) < 100:
                break
    log.info("edgar SIC directory -> %d companies", len(out))
    return out


# Prop shops are invisible in SEC data but they are all exchange members.
MEMBER_DIRECTORIES = [
    "https://www.cmegroup.com/company/clearing-firms.html",
    "https://www.theocc.com/company-information/member-directory",
    "https://www.cboe.com/us/options/membership/trading_permit_holders/",
    "https://www.miaxglobal.com/markets/us-options/miax-options/membership",
]

FIRMISH = re.compile(
    r"\b[A-Z][A-Za-z&'.\-]{1,24}(?:\s+[A-Z][A-Za-z&'.\-]{1,24}){0,4}\s+"
    r"(Trading|Capital|Securities|Partners|Markets|Futures|Options|Group|"
    r"Investments?|Management|Advisors|Brokerage|Holdings|LLC|LP|Inc)\b")


def harvest_member_directories(urls: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    for url in (urls if urls is not None else MEMBER_DIRECTORIES):
        html = get_text(url)
        if not html:
            continue
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        seen = set()
        for m in FIRMISH.finditer(text):
            name = clean_name(m.group(0))
            k = name_key(name)
            if k and k not in seen and len(k) > 4:
                seen.add(k)
                out.append({"name": name, "source": "exchange-members"})
        log.info("member directory %s -> %d names", url.split("/")[2], len(seen))
    return out


HARVESTERS = {
    "edgar-13f": harvest_edgar_13f,
    "edgar-sic": harvest_edgar_sic,
    "members": harvest_member_directories,
}


# ---------------------------------------------------------------------------
# Candidate store
# ---------------------------------------------------------------------------

def load_candidates(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("candidates.json corrupt; starting fresh")
    return {}


def save_candidates(path: Path, cands: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cands, indent=0, sort_keys=True))


def harvest(cands: dict, known_keys: set[str], *,
            sources: list[str] | None = None) -> int:
    """Fold new candidate names into the store. Returns how many were added."""
    added = 0
    for key in (sources or list(HARVESTERS)):
        fn = HARVESTERS.get(key)
        if fn is None:
            continue
        try:
            rows = fn()
        except Exception as exc:
            log.warning("harvester %s failed: %s", key, exc)
            continue
        for row in rows:
            # Normalize here as well as in each harvester, so a source that
            # forgets to clean can't put "ACME TRADING LLC" into the registry.
            name = clean_name(row.get("name", ""))
            k = name_key(name)
            if not k or len(k) < 4 or k in cands or is_known(k, known_keys):
                continue
            rel = relevance(name)
            if rel < 0:
                continue
            cands[k] = {"name": name, "source": row.get("source", key),
                        "relevance": rel, "status": "new",
                        "found_at": datetime.now(timezone.utc).isoformat()}
            added += 1
            if len(cands) >= MAX_CANDIDATES:
                log.warning("candidate cap reached (%d)", MAX_CANDIDATES)
                return added
    return added


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------

def qualify_one(entry: dict) -> dict:
    """Find a live board for a candidate. Mutates and returns the entry."""
    name = entry["name"]
    for domain in domain_guesses(name):
        specs = sniff_careers(domain, max_pages=3)
        for spec in specs:
            if fetch_source(spec):
                entry.update(status="promoted", domain=domain, ats=spec,
                             checked_at=datetime.now(timezone.utc).isoformat())
                log.info("PROMOTED %-44s -> %s", name[:44], spec)
                return entry
    entry.update(status="no-board",
                 checked_at=datetime.now(timezone.utc).isoformat(),
                 attempts=entry.get("attempts", 0) + 1)
    return entry


def qualify(cands: dict, *, budget: int = QUALIFY_BUDGET,
            workers: int = 10) -> int:
    """Work through the backlog, best-looking names first."""
    todo = [e for e in cands.values() if e.get("status") == "new"]
    todo.sort(key=lambda e: (-e.get("relevance", 0), e["name"]))
    todo = todo[:budget]
    if not todo:
        log.info("no candidates to qualify")
        return 0
    log.info("qualifying %d candidates (%d still queued)",
             len(todo), sum(1 for e in cands.values() if e.get("status") == "new"))
    promoted = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(qualify_one, e): e for e in todo}
        for fut in as_completed(futures):
            try:
                e = fut.result()
            except Exception as exc:
                log.warning("qualify failed for %s: %s", futures[fut]["name"], exc)
                futures[fut]["status"] = "no-board"
                continue
            if e.get("status") == "promoted":
                promoted += 1
    return promoted


CATEGORY_BY_SOURCE = {
    "edgar-13f": "fund",
    "edgar-sic-6282": "fund",
    "edgar-sic-6726": "fund",
    "edgar-sic-6211": "bank",
    "edgar-sic-6221": "prop",
    "edgar-sic-6199": "fintech",
    "exchange-members": "prop",
}


def to_registry(cands: dict) -> list[dict]:
    """Promoted candidates, shaped like firms.yaml entries."""
    firms = []
    for entry in cands.values():
        if entry.get("status") != "promoted":
            continue
        name = entry["name"]
        firms.append({
            "name": name,
            "category": CATEGORY_BY_SOURCE.get(entry.get("source", ""), "fund"),
            "regions": [],
            "domain": entry.get("domain", ""),
            "ats": [entry["ats"]] if entry.get("ats") else [],
            "slugs": [slug_for(name)],
            "discovered": True,
            "source": entry.get("source", ""),
        })
    return firms


def run(data_dir: Path, known_keys: set[str], *,
        sources: list[str] | None = None,
        budget: int = QUALIFY_BUDGET, workers: int = 10) -> dict:
    path = data_dir / "candidates.json"
    cands = load_candidates(path)
    before = len(cands)

    added = harvest(cands, known_keys, sources=sources)
    save_candidates(path, cands)

    promoted = qualify(cands, budget=budget, workers=workers)
    save_candidates(path, cands)

    counts: dict[str, int] = {}
    for e in cands.values():
        counts[e.get("status", "?")] = counts.get(e.get("status", "?"), 0) + 1

    stats = {"candidates_before": before, "candidates_now": len(cands),
             "harvested": added, "promoted_this_run": promoted,
             "by_status": counts}
    log.info("discovery: +%d candidates, %d promoted, %s",
             added, promoted, counts)
    return stats
