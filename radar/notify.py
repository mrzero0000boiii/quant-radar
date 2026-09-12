"""
Alerts. Two independent channels, both fire only when the sweep found
something genuinely new:

  github  - opens an Issue in your own repo using the token GitHub Actions
            already provides. No credentials of yours are involved, and
            GitHub emails you about your own issues natively (phone app too).
  email   - SMTP digest. Nicer looking, but needs a mail password.
"""
from __future__ import annotations

import html
import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

log = logging.getLogger("radar.notify")

MAX_IN_EMAIL = 60

CAT_LABEL = {
    "quant-trading": "Quant Trading", "quant-research": "Quant Research",
    "quant-dev": "Quant Dev", "portfolio-mgmt": "Portfolio / Investment",
    "sales-trading": "S&T / Markets", "strats-risk": "Strats / Risk",
    "data-science": "Data / ML", "swe": "Software", "ops-other": "Ops / Other",
    "other": "Other",
}


def _cfg() -> dict | None:
    to = os.environ.get("MAIL_TO", "").strip()
    user = os.environ.get("SMTP_USER", "").strip()
    pw = os.environ.get("SMTP_PASS", "").strip()
    if not (to and user and pw):
        log.info("email not configured (need MAIL_TO, SMTP_USER, SMTP_PASS) - skipping")
        return None
    return {
        "host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "user": user, "password": pw, "to": to,
        "from": os.environ.get("MAIL_FROM", user),
        "site": os.environ.get("SITE_URL", ""),
    }


def _subject(new: list[dict]) -> str:
    targets = [j for j in new if j.get("target")]
    top = max(new, key=lambda j: j.get("score", 0))
    lead = f"{top['firm']} — {top['title'][:58]}"
    if len(new) == 1:
        return f"[radar] {lead}"
    extra = f" +{len(new) - 1} more"
    tag = f"{len(targets)} x 2027" if targets else f"{len(new)} roles"
    return f"[radar] {tag}: {lead}{extra}"


def _text(new: list[dict], site: str) -> str:
    lines = [f"{len(new)} new posting(s) since the last sweep.", ""]
    for j in new[:MAX_IN_EMAIL]:
        bits = [b for b in (j.get("level"), j.get("cycle"),
                            j.get("location") or None) if b]
        lines.append(f"* {j['firm']} — {j['title']}")
        lines.append(f"  {' | '.join(bits)}")
        if j.get("language_risk") == "high":
            lines.append("  ! location likely needs a non-English language")
        if j.get("url"):
            lines.append(f"  {j['url']}")
        lines.append("")
    if len(new) > MAX_IN_EMAIL:
        lines.append(f"...and {len(new) - MAX_IN_EMAIL} more.")
    if site:
        lines.append(f"\nFull board: {site}")
    return "\n".join(lines)


def _html(new: list[dict], site: str) -> str:
    e = html.escape
    groups: dict[str, list[dict]] = {}
    for j in new[:MAX_IN_EMAIL]:
        groups.setdefault(j.get("category", "other"), []).append(j)
    order = sorted(groups, key=lambda c: -max(x.get("score", 0) for x in groups[c]))

    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:680px;color:#12161c">',
        f'<p style="font-size:13px;color:#5a6472;margin:0 0 16px">'
        f'<b style="color:#12161c">{len(new)}</b> new posting(s) since the last sweep.</p>',
    ]
    for cat in order:
        parts.append(
            f'<div style="font:600 11px ui-monospace,Menlo,monospace;letter-spacing:.08em;'
            f'text-transform:uppercase;color:#5a6472;margin:18px 0 8px;'
            f'border-bottom:1px solid #dde3ea;padding-bottom:5px">'
            f'{e(CAT_LABEL.get(cat, cat))}</div>')
        for j in sorted(groups[cat], key=lambda x: -x.get("score", 0)):
            title = e(j["title"])
            link = (f'<a href="{e(j["url"])}" style="color:#0969da;text-decoration:none">'
                    f'{title}</a>') if j.get("url") else title
            chips = []
            if j.get("level") in ("intern", "newgrad"):
                chips.append(("#0969da", j["level"]))
            if j.get("cycle"):
                chips.append(("#5a6472", j["cycle"]))
            if j.get("language_risk") == "high":
                chips.append(("#b35900", "language risk"))
            chip_html = "".join(
                f'<span style="font:10px ui-monospace,Menlo,monospace;color:{c};'
                f'border:1px solid {c};border-radius:4px;padding:1px 5px;margin-right:5px">'
                f'{e(t)}</span>' for c, t in chips)
            parts.append(
                f'<div style="margin:0 0 11px">'
                f'<div style="font-size:14px;font-weight:600">{link}</div>'
                f'<div style="font-size:12px;color:#5a6472;margin-top:2px">'
                f'{e(j["firm"])}{" · " + e(j["location"]) if j.get("location") else ""}</div>'
                f'<div style="margin-top:4px">{chip_html}</div></div>')
    if len(new) > MAX_IN_EMAIL:
        parts.append(f'<p style="font-size:12px;color:#5a6472">'
                     f'…and {len(new) - MAX_IN_EMAIL} more.</p>')
    if site:
        parts.append(f'<p style="margin-top:22px"><a href="{e(site)}" '
                     f'style="color:#0969da;font-size:13px">Open the full board →</a></p>')
    parts.append("</div>")
    return "".join(parts)


def _markdown(new: list[dict], site: str) -> str:
    groups: dict[str, list[dict]] = {}
    for j in new[:MAX_IN_EMAIL]:
        groups.setdefault(j.get("category", "other"), []).append(j)
    order = sorted(groups, key=lambda c: -max(x.get("score", 0) for x in groups[c]))

    out = [f"**{len(new)} new posting(s)** since the last sweep.", ""]
    for cat in order:
        out.append(f"### {CAT_LABEL.get(cat, cat)}")
        out.append("")
        for j in sorted(groups[cat], key=lambda x: -x.get("score", 0)):
            title = j["title"].replace("|", "\\|")
            link = f"[{title}]({j['url']})" if j.get("url") else title
            bits = [j["firm"]]
            if j.get("location"):
                bits.append(j["location"])
            flags = []
            if j.get("target"):
                flags.append("**2027**")
            if j.get("level") in ("intern", "newgrad"):
                flags.append(j["level"])
            if j.get("language_risk") == "high":
                flags.append("language risk")
            tail = f" — {' · '.join(flags)}" if flags else ""
            out.append(f"- {link}  \n  {' · '.join(bits)}{tail}")
        out.append("")
    if len(new) > MAX_IN_EMAIL:
        out.append(f"…and {len(new) - MAX_IN_EMAIL} more.")
    if site:
        out.append(f"\n[Open the full board →]({site})")
    return "\n".join(out)


def post_github_issue(new: list[dict], *, dry_run: bool = False) -> bool:
    """
    Open an Issue on the repo this workflow is running in.

    Uses GITHUB_TOKEN, which Actions injects automatically and which is scoped
    to this repository only - so there is no personal credential to leak, and
    nothing to revoke if the repo is deleted. GitHub emails the repo owner
    about new issues by default, which is the actual delivery mechanism.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not new:
        return False
    if not (token and repo):
        log.info("github issue alerts unavailable (no GITHUB_TOKEN/REPOSITORY)")
        return False

    new = sorted(new, key=lambda j: -j.get("score", 0))
    site = os.environ.get("SITE_URL", "")
    payload = {
        "title": _subject(new).replace("[radar] ", "radar: "),
        "body": _markdown(new, site),
        "labels": ["new-postings"],
    }

    if dry_run:
        print("--- DRY RUN GITHUB ISSUE ---")
        print(payload["title"])
        print(payload["body"][:2000])
        return True

    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "quant-radar",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
        log.info("opened issue #%s with %d new postings",
                 body.get("number"), len(new))
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300] if hasattr(exc, "read") else ""
        log.error("github issue failed: HTTP %s %s", exc.code, detail)
    except Exception as exc:
        log.error("github issue failed: %s", exc)
    return False


def send_digest(new: list[dict], *, dry_run: bool = False) -> bool:
    if not new:
        log.info("no new postings - no email")
        return False
    cfg = _cfg()
    if cfg is None:
        return False

    new = sorted(new, key=lambda j: -j.get("score", 0))
    msg = EmailMessage()
    msg["Subject"] = _subject(new)
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(_text(new, cfg["site"]))
    msg.add_alternative(_html(new, cfg["site"]), subtype="html")

    if dry_run:
        print("--- DRY RUN EMAIL ---")
        print("Subject:", msg["Subject"])
        print(msg.get_body(("plain",)).get_content()[:2000])
        return True

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.starttls()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    except Exception as exc:
        log.error("email send failed: %s", exc)
        return False
    log.info("emailed %d new postings to %s", len(new), cfg["to"])
    return True


def notify_all(new: list[dict], *, dry_run: bool = False) -> dict:
    """
    Fire every channel that is configured. GitHub Issues need no setup and are
    the default; email only runs if SMTP credentials were supplied.
    """
    if not new:
        log.info("no new postings - no alerts")
        return {"github": False, "email": False}
    results = {
        "github": post_github_issue(new, dry_run=dry_run),
        "email": send_digest(new, dry_run=dry_run),
    }
    if not any(results.values()):
        log.warning("%d new postings but no alert channel is configured", len(new))
    return results
