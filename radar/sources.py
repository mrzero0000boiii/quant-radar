"""
ATS adapters.

Every adapter exposes:
    fetch(ident) -> list[RawJob]      # [] means "board exists but empty" OR "no board"
    probe(ident) -> bool              # cheap existence check

RawJob is a plain dict with normalized keys:
    title, location, url, posted_at (ISO str or None), department, employment_type,
    external_id, description (optional, often empty)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .net import get_json, get_text, post_json

log = logging.getLogger("radar.sources")

# ---------------------------------------------------------------------------
# Generic record normalization
# ---------------------------------------------------------------------------

TITLE_KEYS = ("title", "name", "text", "jobTitle", "job_title", "position", "positionTitle")
URL_KEYS = ("absolute_url", "hostedUrl", "jobUrl", "applyUrl", "apply_url", "url",
            "careers_url", "link", "href", "externalPath", "ref", "canonicalUrl")
LOC_KEYS = ("location", "locations", "locationsText", "locationName", "city", "office",
            "offices", "primaryLocation", "jobLocation", "location_name", "workplace")
DATE_KEYS = ("updated_at", "createdAt", "publishedAt", "postedOn", "posted_date", "postedDate",
             "releasedDate", "created_at", "published_on", "publishedDate", "firstPublished",
             "datePosted", "date", "openedDate", "startDate")
DEPT_KEYS = ("department", "departmentName", "team", "category", "function", "businessUnit")
TYPE_KEYS = ("employmentType", "employment_type", "commitment", "type", "jobType",
             "typeOfEmployment", "contractType")
ID_KEYS = ("id", "jobId", "shortcode", "uuid", "reqId", "requisitionId", "externalId", "slug")


def _first(rec: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k in rec and rec[k] not in (None, "", [], {}):
            return rec[k]
    return None


def _flatten_location(val: Any) -> str:
    """Turn whatever an ATS calls a location into a readable string."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in ("name", "locationsText", "text", "label", "displayName"):
            if isinstance(val.get(k), str) and val[k].strip():
                return val[k].strip()
        parts = [str(val.get(k)) for k in ("city", "region", "state", "country", "countryName")
                 if val.get(k)]
        return ", ".join(dict.fromkeys(parts))
    if isinstance(val, (list, tuple)):
        parts = [_flatten_location(v) for v in val]
        parts = [p for p in parts if p]
        return " | ".join(dict.fromkeys(parts))
    return str(val)


def _parse_date(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        # epoch seconds or millis
        ts = float(val)
        if ts > 1e11:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    s = str(val).strip()
    if not s:
        return None
    # Workday says things like "Posted 3 Days Ago" / "Posted Today"
    m = re.search(r"(\d+)\s*\+?\s*day", s, re.I)
    if m:
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))).isoformat()
    if re.search(r"posted\s+today|^today$", s, re.I):
        return datetime.now(timezone.utc).isoformat()
    if re.search(r"posted\s+yesterday", s, re.I):
        from datetime import timedelta
        return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    s2 = s.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d",
                "%Y-%m-%dT%H:%M:%S", "%b %d, %Y", "%d %b %Y"):
        try:
            dt = datetime.fromisoformat(s2) if fmt is None else datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None


def normalize(rec: dict, *, base_url: str = "") -> dict | None:
    """Map an arbitrary ATS record onto our schema. None if it isn't job-like."""
    if not isinstance(rec, dict):
        return None
    title = _first(rec, TITLE_KEYS)
    if not isinstance(title, str) or not title.strip():
        return None
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > 200:
        return None

    url = _first(rec, URL_KEYS)
    if isinstance(url, dict):
        url = url.get("url") or url.get("href")
    url = str(url) if url else ""
    if url and not url.startswith("http"):
        url = base_url.rstrip("/") + "/" + url.lstrip("/") if base_url else ""

    loc = _flatten_location(_first(rec, LOC_KEYS))
    dept = _flatten_location(_first(rec, DEPT_KEYS))
    etype = _flatten_location(_first(rec, TYPE_KEYS))
    posted = _parse_date(_first(rec, DATE_KEYS))
    ext_id = _first(rec, ID_KEYS)

    return {
        "title": title,
        "location": loc,
        "url": url,
        "posted_at": posted,
        "department": dept,
        "employment_type": etype,
        "external_id": str(ext_id) if ext_id is not None else "",
    }


def harvest(payload: Any, *, base_url: str = "", list_keys: Iterable[str] = ()) -> list[dict]:
    """
    Find the job array anywhere in a JSON payload and normalize it.
    Tries named keys first, then falls back to the largest job-like list found.
    """
    if payload is None:
        return []
    candidates: list[list] = []

    if isinstance(payload, list):
        candidates.append(payload)
    elif isinstance(payload, dict):
        for k in list(list_keys) + ["jobs", "offers", "content", "postings", "results",
                                    "data", "items", "jobPostings", "positions", "openings"]:
            v = payload.get(k)
            if isinstance(v, list):
                candidates.append(v)
            elif isinstance(v, dict):
                for vv in v.values():
                    if isinstance(vv, list):
                        candidates.append(vv)
        if not candidates:  # deep scan, one level
            for v in payload.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    candidates.append(v)

    best: list[dict] = []
    for cand in candidates:
        out = []
        for rec in cand:
            n = normalize(rec, base_url=base_url) if isinstance(rec, dict) else None
            if n:
                out.append(n)
        if len(out) > len(best):
            best = out
    return best


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------

class Adapter:
    name = "base"

    def urls(self, ident: str) -> list[str]:
        raise NotImplementedError

    def fetch(self, ident: str) -> list[dict]:
        raise NotImplementedError

    def probe(self, ident: str) -> bool:
        return bool(self.fetch(ident))


class Greenhouse(Adapter):
    name = "greenhouse"

    def fetch(self, ident: str) -> list[dict]:
        for u in (f"https://boards-api.greenhouse.io/v1/boards/{ident}/jobs?content=false",
                  f"https://api.greenhouse.io/v1/boards/{ident}/embed/jobs"):
            data = get_json(u)
            jobs = harvest(data, list_keys=("jobs",))
            if jobs:
                for j in jobs:
                    if not j["url"]:
                        j["url"] = f"https://boards.greenhouse.io/{ident}/jobs/{j['external_id']}"
                return jobs
        return []


class Lever(Adapter):
    name = "lever"

    def fetch(self, ident: str) -> list[dict]:
        data = get_json(f"https://api.lever.co/v0/postings/{ident}?mode=json")
        jobs = harvest(data)
        # Lever nests location/team under `categories`
        if isinstance(data, list):
            for raw, j in zip(data, jobs):
                cats = raw.get("categories") or {}
                if isinstance(cats, dict):
                    j["location"] = j["location"] or _flatten_location(cats.get("location"))
                    j["department"] = j["department"] or _flatten_location(
                        cats.get("team") or cats.get("department"))
                    j["employment_type"] = j["employment_type"] or _flatten_location(
                        cats.get("commitment"))
        return jobs


class Ashby(Adapter):
    name = "ashby"

    def fetch(self, ident: str) -> list[dict]:
        data = get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{ident}?includeCompensation=false")
        jobs = harvest(data, list_keys=("jobs",))
        for j in jobs:
            if not j["url"]:
                j["url"] = f"https://jobs.ashbyhq.com/{ident}/{j['external_id']}"
        return jobs


class SmartRecruiters(Adapter):
    name = "smartrecruiters"

    def fetch(self, ident: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        for _ in range(12):  # up to 1200 postings
            data = get_json(
                f"https://api.smartrecruiters.com/v1/companies/{ident}/postings"
                f"?limit=100&offset={offset}")
            if not data:
                break
            page = harvest(data, list_keys=("content",))
            if not page:
                break
            for raw, j in zip(data.get("content", []), page):
                if not j["url"]:
                    j["url"] = (f"https://jobs.smartrecruiters.com/{ident}/"
                                f"{raw.get('id', '')}")
            out.extend(page)
            offset += 100
            if offset >= int(data.get("totalFound", 0) or 0):
                break
        return out


class Recruitee(Adapter):
    name = "recruitee"

    def fetch(self, ident: str) -> list[dict]:
        data = get_json(f"https://{ident}.recruitee.com/api/offers/")
        return harvest(data, list_keys=("offers",))


class Workable(Adapter):
    name = "workable"

    def fetch(self, ident: str) -> list[dict]:
        data = get_json(
            f"https://apply.workable.com/api/v1/widget/accounts/{ident}?details=true")
        jobs = harvest(data, list_keys=("jobs",))
        if isinstance(data, dict):
            for raw, j in zip(data.get("jobs", []), jobs):
                if not j["location"]:
                    j["location"] = ", ".join(
                        x for x in (raw.get("city"), raw.get("country")) if x)
                if not j["url"]:
                    j["url"] = (f"https://apply.workable.com/{ident}/j/"
                                f"{raw.get('shortcode', '')}")
        return jobs


class Personio(Adapter):
    name = "personio"

    def fetch(self, ident: str) -> list[dict]:
        for host in (f"https://{ident}.jobs.personio.de/search.json",
                     f"https://{ident}.jobs.personio.com/search.json"):
            data = get_json(host)
            jobs = harvest(data)
            if jobs:
                return jobs
        return []


class Workday(Adapter):
    """
    Workday CxS. ident is 'tenant' or 'tenant/site' or 'tenant/site/wdN'.
    Without an explicit site we probe a list of common site names across wd1..wd5.
    """
    name = "workday"
    # Full matrix is 12 sites x 6 pods = 72 POSTs per tenant. That is fine when
    # the registry names an exact tenant, and ruinous when we are guessing, so
    # blind probing uses the short lists only (4 x 3 = 12).
    SITES = ("External", "External_Career_Site", "Careers", "careers", "External_Careers",
             "Global_Careers", "en-US", "ExternalCareerSite", "Professional",
             "External_Experienced", "Search", "jobs")
    PODS = ("wd1", "wd2", "wd3", "wd5", "wd103", "wd12")
    BLIND_SITES = ("External", "External_Career_Site", "Careers", "en-US")
    BLIND_PODS = ("wd1", "wd3", "wd5")

    @staticmethod
    def _endpoints(ident: str, *, blind: bool = False) -> list[str]:
        parts = ident.split("/")
        tenant = parts[0]
        named = len(parts) > 1 and parts[1]
        all_sites = Workday.BLIND_SITES if (blind and not named) else Workday.SITES
        all_pods = Workday.BLIND_PODS if (blind and not named) else Workday.PODS
        sites = [parts[1]] if named else list(all_sites)
        pods = [parts[2]] if len(parts) > 2 and parts[2] else list(all_pods)
        return [
            f"https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            for pod in pods for site in sites
        ]

    def _page(self, endpoint: str, offset: int) -> Any:
        return post_json(endpoint, {
            "appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "",
        }, headers={"Accept": "application/json"})

    def fetch(self, ident: str, *, blind: bool = False) -> list[dict]:
        for endpoint in self._endpoints(ident, blind=blind):
            first = self._page(endpoint, 0)
            if not isinstance(first, dict) or not first.get("jobPostings"):
                continue
            tenant = endpoint.split("/wday/cxs/")[1].split("/")[0]
            host = endpoint.split("/wday/cxs/")[0]
            site = endpoint.rstrip("/jobs").split("/")[-1]
            base = f"{host}/{site}"
            total = int(first.get("total") or 0)
            pages = [first]
            for off in range(20, min(total, 600), 20):
                p = self._page(endpoint, off)
                if not p:
                    break
                pages.append(p)
            out: list[dict] = []
            for p in pages:
                jobs = harvest(p, base_url=base, list_keys=("jobPostings",))
                for raw, j in zip(p.get("jobPostings", []), jobs):
                    if not j["location"]:
                        j["location"] = _flatten_location(raw.get("locationsText"))
                    if not j["posted_at"]:
                        j["posted_at"] = _parse_date(raw.get("postedOn"))
                out.extend(jobs)
            if out:
                log.info("workday resolved %s -> %s", ident, endpoint)
                out[0]["_endpoint"] = endpoint
                return out
        return []


class Generic(Adapter):
    """Try a list of candidate JSON career endpoints and auto-map whatever comes back."""
    name = "generic"

    TEMPLATES = (
        "https://{d}/api/jobs",
        "https://{d}/api/positions",
        "https://{d}/jobs.json",
        "https://{d}/careers.json",
        "https://{d}/api/careers/jobs",
        "https://careers.{d}/api/jobs",
        "https://{d}/wp-json/wp/v2/jobs?per_page=100",
    )

    def fetch(self, ident: str) -> list[dict]:
        urls = [ident] if ident.startswith("http") else [
            t.format(d=ident) for t in self.TEMPLATES]
        for u in urls:
            data = get_json(u)
            jobs = harvest(data, base_url=u)
            if len(jobs) >= 2:
                return jobs
        return []


class HtmlBoard(Adapter):
    """
    Last resort for firms that render listings on their own domain with no JSON
    API behind them (Citadel Securities, for one). Pulls anchor tags that look
    like job links. Deliberately strict: it would rather return nothing than
    fill the board with navigation links.
    """
    name = "html"

    ANCHOR = re.compile(
        r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
    JOBBISH_HREF = re.compile(
        r"/(job|jobs|career|careers|position|positions|opening|openings|vacanc|"
        r"role|roles|apply)[-/_]", re.I)
    HAS_ID = re.compile(r"(\d{3,}|[a-f0-9]{8}-[a-f0-9]{4})")
    TAG = re.compile(r"<[^>]+>")
    NAV_NOISE = re.compile(
        r"^(apply|apply now|learn more|read more|view|view all|see all|all jobs|"
        r"back|next|previous|home|about|contact|search|filter|menu|careers|jobs|"
        r"life at|our people|benefits|diversity|privacy|cookies|terms|login|"
        r"sign in|share|email|linkedin|twitter|facebook|instagram)$", re.I)

    def fetch(self, ident: str) -> list[dict]:
        url = ident if ident.startswith("http") else f"https://{ident}"
        html = get_text(url)
        if not html:
            return []
        base = "/".join(url.split("/")[:3])
        out: dict[str, dict] = {}
        for href, inner in self.ANCHOR.findall(html):
            title = re.sub(r"\s+", " ", self.TAG.sub(" ", inner)).strip()
            if not (3 < len(title) <= 120) or self.NAV_NOISE.match(title):
                continue
            if not (self.JOBBISH_HREF.search(href) and self.HAS_ID.search(href)):
                continue
            if not re.search(r"[a-zA-Z]{3}", title):
                continue
            full = href if href.startswith("http") else base + "/" + href.lstrip("/")
            out.setdefault(full, {
                "title": title, "location": "", "url": full, "posted_at": None,
                "department": "", "employment_type": "",
                "external_id": (self.HAS_ID.search(href) or [""])[0],
            })
        jobs = list(out.values())
        # A real board has several distinct postings. One or two matches is
        # almost always a footer link that slipped through.
        return jobs if len(jobs) >= 3 else []


ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (
        Greenhouse(), Lever(), Ashby(), SmartRecruiters(),
        Recruitee(), Workable(), Personio(), Workday(), Generic(), HtmlBoard(),
    )
}

# ---------------------------------------------------------------------------
# Careers-page sniffing
#
# Guessing slugs from a company name fails on exactly the firms you most want:
# Hudson River Trading's public board is "hrttalentcommunity", which no name
# derivation produces. Their careers page says so in an href. So: read the page.
# ---------------------------------------------------------------------------

ATS_URL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]{2,60})"), "greenhouse"),
    # The embed snippet is the most common form on a firm's own careers page,
    # and it comes as .../embed/job_board/js?for=SLUG - the /js is easy to miss.
    (re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]{2,60})"),
     "greenhouse"),
    (re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]{2,60})"), "greenhouse"),
    (re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]{2,60})"), "lever"),
    (re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]{2,60})"), "lever"),
    (re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]{2,60})"), "ashby"),
    (re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]{2,60})"), "ashby"),
    (re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]{2,60})"),
     "smartrecruiters"),
    (re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]{2,60})"),
     "smartrecruiters"),
    (re.compile(r"([A-Za-z0-9-]{2,60})\.recruitee\.com"), "recruitee"),
    (re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]{2,60})"), "workable"),
    (re.compile(r"([A-Za-z0-9-]{2,60})\.jobs\.personio\.(?:de|com)"), "personio"),
]

# Workday URLs carry an optional locale segment before the site, and the site
# itself can be a single character (Citi's is literally "2"). A loose locale
# group plus a 2-char minimum on the site makes the locale win - so the locale
# pattern is pinned to real locale shapes and the site allows one character.
WORKDAY_URL = re.compile(
    r"([A-Za-z0-9-]{2,40})\.(wd\d{1,3})\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}(?:[-_][A-Za-z]{2})?/)?([A-Za-z0-9_-]{1,60})")

# Slugs that appear on careers pages but belong to the vendor, not the firm.
SLUG_BLOCKLIST = {"embed", "job_board", "jobs", "job", "careers", "career", "www",
                  "api", "boards", "v1", "static", "assets", "images", "css", "js",
                  "greenhouse", "lever", "ashby", "workday", "smartrecruiters"}

CAREERS_PATHS = ("/careers", "/careers/", "/en/careers", "/careers/open-roles",
                 "/careers/jobs", "/jobs", "/join-us", "/company/careers", "")


def specs_from_html(html: str) -> list[str]:
    """Extract ATS specs from a page's markup."""
    specs: list[str] = []
    for pattern, kind in ATS_URL_PATTERNS:
        for slug in pattern.findall(html or ""):
            slug = slug.strip("-_.")
            if not slug or slug.lower() in SLUG_BLOCKLIST or len(slug) < 2:
                continue
            spec = f"{kind}:{slug}"
            if spec not in specs:
                specs.append(spec)
    for tenant, pod, site in WORKDAY_URL.findall(html or ""):
        if tenant.lower() in SLUG_BLOCKLIST:
            continue
        spec = f"workday:{tenant}/{site}/{pod}"
        if spec not in specs:
            specs.append(spec)
    return specs


def sniff_careers(domain: str, *, max_pages: int = 4) -> list[str]:
    """
    Fetch a firm's careers page(s) and read off whatever ATS it actually uses.
    Cheap (a handful of GETs) and far higher-yield than guessing slugs.
    """
    if not domain:
        return []
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    found: list[str] = []
    pages = 0
    for path in CAREERS_PATHS:
        if pages >= max_pages:
            break
        for host in (f"https://www.{domain}", f"https://{domain}"):
            if domain.startswith("www."):
                host = f"https://{domain}"
            html = get_text(host + path)
            pages += 1
            if html is None:
                continue
            for spec in specs_from_html(html):
                if spec not in found:
                    found.append(spec)
            if found:
                return found
            break  # host reachable but no ATS on this path; try the next path
    return found

# Order matters: cheapest + highest-hit-rate first. 'html' and 'generic' are
# never blind-probed - they only run when the registry names an exact URL.
PROBE_ORDER = ("greenhouse", "lever", "ashby", "smartrecruiters",
               "recruitee", "workable", "personio", "workday")


def fetch_source(spec: str, *, blind: bool = False) -> list[dict]:
    """
    spec is 'adapter:ident', e.g. 'greenhouse:citadel' or 'workday:jpmc/External'.

    blind=True means this spec is a guess from the discovery matrix rather than
    something the registry asserted, so expensive adapters should use their
    cheap probe path.
    """
    if ":" not in spec:
        return []
    kind, ident = spec.split(":", 1)
    adapter = ADAPTERS.get(kind)
    if adapter is None:
        if kind == "custom":
            adapter = ADAPTERS["generic"]
        else:
            return []
    try:
        if blind and kind == "workday":
            jobs = adapter.fetch(ident, blind=True)
        else:
            jobs = adapter.fetch(ident)
    except Exception as exc:  # never let one board kill the run
        log.warning("adapter %s failed on %s: %s", kind, ident, exc)
        return []
    for j in jobs:
        j["source"] = spec
    return jobs
