"""
Turns a raw posting into something worth (or not worth) looking at.

Three independent judgements:
  level     - intern / newgrad / entry / experienced / unknown
  category  - what kind of seat it is
  language  - whether the location likely needs a non-English language
Plus a priority score used for default sort.
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Level
# ---------------------------------------------------------------------------

INTERN_PAT = re.compile(
    r"\b(intern|internship|interns|summer analyst|summer associate|off[- ]cycle|"
    r"placement|industrial placement|co[- ]?op|coop|trainee|apprentice|"
    r"spring (week|insight|programme|program)|insight (week|programme|program|day)|"
    r"work experience|penultimate|campus|student|undergraduate|graduate intern|"
    r"summer (programme|program|intern|scheme)|vacation scheme|praktikum|stage\b|"
    r"working student|werkstudent)\b", re.I)

NEWGRAD_PAT = re.compile(
    r"\b(new ?grad(uate)?|graduate (programme|program|scheme|analyst|developer|engineer|"
    r"trader|role|opportunit)|campus hire|analyst (programme|program|rotational)|"
    r"rotational (programme|program|analyst)|entry[- ]level|class of 20\d\d|"
    r"early career|emerging talent|junior|associate programme|associate program|"
    r"trainee programme|graduate$)", re.I)

SENIOR_PAT = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|head of|director|managing|vice president|"
    r"\bvp\b|\bsvp\b|\bevp\b|executive|chief|partner|manager|architect|"
    r"distinguished|fellow|expert|specialist ii+|ii+\b|iii\b|\biv\b)\b", re.I)

YEARS_PAT = re.compile(r"\b([3-9]|1\d)\+?\s*(\+|years?|yrs?)\b", re.I)
CYCLE_PAT = re.compile(r"\b(20(2[5-9]|3[0-5]))\b")


def detect_level(title: str, employment_type: str = "", extra: str = "") -> str:
    blob = f"{title} {employment_type} {extra}"
    if INTERN_PAT.search(blob):
        return "intern"
    if NEWGRAD_PAT.search(blob):
        return "newgrad"
    if SENIOR_PAT.search(title) or YEARS_PAT.search(blob):
        return "experienced"
    if re.search(r"\banalyst\b", title, re.I) and not SENIOR_PAT.search(title):
        return "entry"
    return "unknown"


def detect_cycle(title: str, extra: str = "") -> str | None:
    blob = f"{title} {extra}"
    m = CYCLE_PAT.search(blob)
    if m:
        return m.group(1)
    # "SA27", "Summer '27", "FY27"
    m = re.search(r"\b(?:sa|fy|summer)\s*'?\s*(2[5-9]|3[0-5])\b", blob, re.I)
    return f"20{m.group(1)}" if m else None


# ---------------------------------------------------------------------------
# The target: 2027 summer cycle
#
# This is the whole point of the tracker, so it gets its own flag rather than
# being something you have to reconstruct from level + cycle in the UI.
# Roll these forward a year when the 2028 cycle opens.
# ---------------------------------------------------------------------------

TARGET_CYCLES = {"2027", "2028"}
PAST_CYCLES = {"2020", "2021", "2022", "2023", "2024", "2025", "2026"}

SUMMER_PAT = re.compile(
    r"\b(summer (analyst|associate|intern|internship|programme|program|scheme)|"
    r"summer\b.*\b(intern|analyst|associate)|"
    r"(intern|analyst|associate)\b.*\bsummer)\b", re.I)


def is_summer_seat(title: str, extra: str = "") -> bool:
    return bool(SUMMER_PAT.search(f"{title} {extra}"))


def is_target(job: dict) -> bool:
    """
    A seat Daniel could actually take: an intern / summer analyst / summer
    associate / graduate seat for the 2027 cycle (or 2028).

    An undated internship counts. Firms very often post "Quantitative Trading
    Intern" with no year, and it is always the cycle currently recruiting -
    excluding those would drop most of the board. Only an explicitly past year
    disqualifies.
    """
    if job.get("level") not in ("intern", "newgrad"):
        return False
    cycle = job.get("cycle")
    if cycle and cycle in PAST_CYCLES:
        return False
    if cycle and cycle not in TARGET_CYCLES:
        return False
    return True


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------

# NOTE: these are deliberately *stem* patterns with a leading \b and NO trailing \b.
# "data scien" has to match "Data Scientist", "trad" has to match "Trading" and
# "Trader". Adding a closing boundary silently breaks every one of them.
CATEGORY_RULES: list[tuple[str, re.Pattern]] = [
    ("quant-trading", re.compile(
        r"\b(quant(itative)?[ -]?trad|trader|trading (intern|analyst|associate|role)|"
        r"market mak|systematic trad|algo(rithmic)? trad|options trad|prop(rietary)? trad|"
        r"execution trad|derivatives trad)", re.I)),
    ("quant-research", re.compile(
        r"\b(quant(itative)?[ -]?research|quant(itative)? analyst|\bqr\b|alpha research|"
        r"signal research|statistical research|quant(itative)? strateg|researcher|"
        r"systematic research|factor research|predictive model|machine learning research)", re.I)),
    ("quant-dev", re.compile(
        r"\b(quant(itative)? (developer|engineer|technolog)|\bqd\b|low[ -]latency|"
        r"trading system|trading (infrastructure|technolog)|c\+\+ (developer|engineer)|"
        r"fpga|hardware engineer|core (dev|engineer)|execution (platform|engineer)|"
        r"pricing (engineer|developer))", re.I)),
    ("portfolio-mgmt", re.compile(
        r"\b(portfolio manage|portfolio implementation|portfolio construction|"
        r"portfolio analyst|\bpm\b team|investment (analyst|associate|professional)|"
        r"fundamental analyst|equity research|credit analyst|multi[- ]?strategy|"
        r"asset allocation|fund management)", re.I)),
    ("sales-trading", re.compile(
        r"\b(sales (and|&) trading|s&t|fixed income|\bficc\b|equities (sales|trading|division)|"
        r"\brates\b|credit trading|fx (trading|sales)|commodities|structuring|"
        r"global markets|capital markets|securities division|flow trading|"
        r"delta one|prime (brokerage|services)|corporate treasury)", re.I)),
    ("strats-risk", re.compile(
        r"\b(strat\b|strats\b|risk (management|analyst|quant|model)|model (validation|risk)|"
        r"market risk|credit risk|counterparty risk|valuation|xva|pricing quant|"
        r"financial engineer|derivatives (analyst|quant))", re.I)),
    ("data-science", re.compile(
        r"\b(data scien|data engineer|machine learning|\bml\b engineer|\bai\b engineer|"
        r"applied scien|data analyst|analytics engineer|research engineer|"
        r"deep learning|nlp engineer)", re.I)),
    ("swe", re.compile(
        r"\b(software (engineer|developer|dev)\b|\bswe\b|backend|back[- ]end|frontend|"
        r"front[- ]end|full[ -]?stack|platform engineer|infrastructure engineer|"
        r"site reliability|\bsre\b|devops|systems engineer|security engineer|"
        r"cloud engineer|technology (analyst|intern|graduate))", re.I)),
    ("ops-other", re.compile(
        r"\b(operations|middle office|back office|trade support|settlement|"
        r"business analyst|product manager|treasury|finance rotational)", re.I)),
]

# Hard excludes: corporate functions that will never be a quant seat.
# Same stem rule as above - no trailing \b.
EXCLUDE_PAT = re.compile(
    r"\b(recruit|talent acquisition|human resources|\bhr\b|people (team|operations|partner)|"
    r"marketing|communications|public relations|social media|copywriter|"
    r"graphic design|ux research|legal counsel|paralegal|attorney|"
    r"facilities|receptionist|executive assistant|office manager|office coordinator|"
    r"janitor|chef|culinary|barista|catering|security guard|driver\b|"
    r"account executive|sales development|\bsdr\b|\bbdr\b|customer success|"
    r"customer support|help ?desk|technical support|call cent|claims|underwrit|"
    r"tax (analyst|manager|associate)|payroll|accounts payable|bookkeep|auditor|"
    r"business development|real estate|procurement|supply chain|warehouse|"
    r"physician|nurse)", re.I)

# Consulted only when nothing above matched. Catches the many postings titled
# just "Off-Cycle Internship - Trading" or "Spring Insight Week - Markets",
# which are exactly the ones worth seeing early.
FALLBACK_RULES: list[tuple[str, re.Pattern]] = [
    ("quant-research", re.compile(r"\b(quant|alpha|signal|stochastic|econometric)", re.I)),
    ("sales-trading", re.compile(
        r"\b(trading|trader|markets|securities|derivativ|equit|bond|treasur|"
        r"investment bank|\bib\b|sales and trading)", re.I)),
    ("portfolio-mgmt", re.compile(
        r"\b(portfolio|investment|asset manage|hedge fund|private (equity|credit)|"
        r"fund\b|capital markets)", re.I)),
    ("swe", re.compile(r"\b(engineer|developer|programm|technolog|software|comput)", re.I)),
    ("data-science", re.compile(r"\b(data|analytic|statistic|research)", re.I)),
]


def detect_category(title: str, department: str = "") -> str:
    blob = f"{title} {department}"
    for name, pat in CATEGORY_RULES:
        if pat.search(blob):
            return name
    for name, pat in FALLBACK_RULES:
        if pat.search(blob):
            return name
    return "other"


def is_excluded(title: str, department: str = "") -> bool:
    return bool(EXCLUDE_PAT.search(f"{title} {department}"))


# ---------------------------------------------------------------------------
# Language risk
# ---------------------------------------------------------------------------

HIGH_RISK_LOCS = {
    "tokyo", "japan", "osaka", "seoul", "korea", "shanghai", "beijing", "shenzhen",
    "guangzhou", "hangzhou", "china", "taipei", "taiwan", "bangkok", "thailand",
    "hanoi", "ho chi minh", "vietnam", "jakarta", "indonesia", "moscow", "russia",
    "seoul-si", "chiyoda", "minato",
}
MED_RISK_LOCS = {
    "paris", "france", "frankfurt", "munich", "berlin", "germany", "madrid", "spain",
    "barcelona", "milan", "rome", "italy", "sao paulo", "são paulo", "rio de janeiro",
    "brazil", "brasil", "mexico city", "mexico", "warsaw", "poland", "istanbul",
    "turkey", "lisbon", "porto", "portugal", "bogota", "colombia", "santiago",
    "buenos aires", "seoul", "vienna", "austria", "prague", "budapest", "bucharest",
}
LOW_RISK_LOCS = {
    "hong kong", "zurich", "geneva", "switzerland", "amsterdam", "netherlands",
    "brussels", "belgium", "stockholm", "copenhagen", "oslo", "helsinki",
}

CJK = re.compile(r"[぀-ヿ一-鿿가-힯]")
LANG_REQ = re.compile(
    r"\b(japanese|mandarin|cantonese|korean|native (speaker|level)|bilingual|"
    r"fluent in (japanese|mandarin|chinese|korean|german|french|spanish|portuguese|dutch))\b",
    re.I)


def language_risk(location: str, title: str = "") -> str:
    blob = f"{location} {title}"
    if CJK.search(blob) or LANG_REQ.search(blob):
        return "high"
    loc = _fold(location)
    if any(k in loc for k in HIGH_RISK_LOCS):
        return "high"
    if any(k in loc for k in MED_RISK_LOCS):
        return "medium"
    if any(k in loc for k in LOW_RISK_LOCS):
        return "low"
    return "none"


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


# ---------------------------------------------------------------------------
# Region tagging (for the location filter)
# ---------------------------------------------------------------------------

REGION_MAP = [
    ("us", ("new york", "nyc", "chicago", "austin", "boston", "san francisco", "sf bay",
            "seattle", "miami", "houston", "dallas", "atlanta", "philadelphia", "jersey city",
            "stamford", "greenwich", "charlotte", "los angeles", "denver", "washington",
            "united states", "usa", " ny", " il", " tx", " ca,", " ma,", " nj", "florida",
            "jupiter", "princeton", "salt lake")),
    ("uk", ("london", "united kingdom", "england", "manchester", "edinburgh", "bristol",
            "cambridge", "oxford", "leeds", "glasgow")),
    ("eu", ("amsterdam", "netherlands", "paris", "france", "frankfurt", "germany", "munich",
            "berlin", "dublin", "ireland", "zurich", "geneva", "switzerland", "brussels",
            "belgium", "madrid", "spain", "milan", "italy", "stockholm", "sweden",
            "copenhagen", "denmark", "oslo", "norway", "helsinki", "finland", "warsaw",
            "poland", "lisbon", "portugal", "vienna", "austria", "prague", "budapest",
            "luxembourg", "rotterdam", "eindhoven", "malta", "cyprus", "bucharest",
            "vilnius", "tallinn", "estonia", "lithuania")),
    ("apac", ("singapore", "hong kong", "tokyo", "japan", "shanghai", "beijing", "shenzhen",
              "sydney", "melbourne", "australia", "seoul", "korea", "taipei", "taiwan",
              "bangkok", "kuala lumpur", "jakarta", "manila", "auckland", "new zealand",
              "hanoi", "ho chi minh", "china")),
    ("india", ("mumbai", "bangalore", "bengaluru", "gurgaon", "gurugram", "hyderabad",
               "delhi", "noida", "pune", "chennai", "india")),
    ("mena", ("dubai", "abu dhabi", "uae", "riyadh", "saudi", "doha", "qatar", "tel aviv",
              "israel", "bahrain", "istanbul", "turkey")),
    ("canada", ("toronto", "montreal", "vancouver", "calgary", "ottawa", "waterloo", "canada")),
    ("latam", ("sao paulo", "são paulo", "rio de janeiro", "brazil", "mexico city", "mexico",
               "bogota", "santiago", "buenos aires", "lima")),
]


def detect_regions(location: str) -> list[str]:
    loc = _fold(location)
    if not loc:
        return []
    out = [name for name, keys in REGION_MAP if any(k in loc for k in keys)]
    if "remote" in loc and not out:
        out.append("remote")
    return out


# ---------------------------------------------------------------------------
# Priority score
# ---------------------------------------------------------------------------

LEVEL_WEIGHT = {"intern": 60, "newgrad": 45, "entry": 30, "unknown": 8, "experienced": 0}
CATEGORY_WEIGHT = {
    "quant-trading": 40, "quant-research": 38, "quant-dev": 30,
    "portfolio-mgmt": 26, "sales-trading": 24, "strats-risk": 20,
    "data-science": 14, "swe": 12, "ops-other": 4, "other": 0,
}
FIRM_WEIGHT = {"prop": 20, "fund": 18, "bank": 12, "exchange": 8, "crypto": 6, "fintech": 5}
LANG_PENALTY = {"high": -25, "medium": -8, "low": -2, "none": 0}


def score(job: dict) -> int:
    s = 0
    s += LEVEL_WEIGHT.get(job.get("level", "unknown"), 0)
    s += CATEGORY_WEIGHT.get(job.get("category", "other"), 0)
    s += FIRM_WEIGHT.get(job.get("firm_category", ""), 0)
    s += LANG_PENALTY.get(job.get("language_risk", "none"), 0)
    if job.get("target"):
        s += 40
    if job.get("cycle") in TARGET_CYCLES:
        s += 20          # says the year outright
    if job.get("summer"):
        s += 10
    if job.get("cycle") in PAST_CYCLES:
        s -= 60          # a leftover posting from a closed cycle
    if job.get("applied"):
        s -= 100
    return s


def enrich(job: dict) -> dict:
    """Attach every derived field. Mutates and returns the job."""
    title = job.get("title", "")
    dept = job.get("department", "")
    etype = job.get("employment_type", "")
    loc = job.get("location", "")

    job["level"] = detect_level(title, etype, dept)
    job["category"] = detect_category(title, dept)
    job["excluded"] = is_excluded(title, dept)
    job["language_risk"] = language_risk(loc, title)
    job["regions"] = detect_regions(loc)
    job["cycle"] = detect_cycle(title, dept)
    job["summer"] = is_summer_seat(title, dept)
    job["target"] = is_target(job)
    job["score"] = score(job)
    return job


def keep(job: dict, *, include_experienced: bool = False) -> bool:
    """Default relevance gate."""
    if job.get("excluded"):
        return False
    if job.get("target"):
        return True     # never filter out the thing we're here for
    if job.get("category") == "other" and job.get("level") not in ("intern", "newgrad"):
        return False
    if not include_experienced and job.get("level") == "experienced":
        return False
    if job.get("cycle") in PAST_CYCLES and job.get("level") in ("intern", "newgrad"):
        return False    # a summer-2025 posting nobody took down
    return True
