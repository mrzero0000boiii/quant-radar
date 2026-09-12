# quant-radar

Sweeps the job boards of 794 trading firms, hedge funds, banks, exchanges and
fintechs every 4 hours, diffs the result against the last sweep, publishes a
filterable board to GitHub Pages, and emails you the moment something new shows up.

```
prop / market makers   143      banks & brokers        127
hedge funds & AM       280      exchanges & infra      105
fintech                 82      crypto trading          57
```

Tuned for the **Summer 2027 cycle**: internships, summer analyst and summer
associate seats, and graduate programmes. Those are the default view and always
email you; everything else is one toggle away.

Nothing here is generated. Every posting comes from the firm's own applicant
tracking system, and every link goes to that firm's real application page.

It runs on GitHub Actions, so it keeps sweeping whether or not your laptop is on.

**The firm list grows by itself.** A second weekly workflow harvests candidate
firms from SEC EDGAR (every 13F filer — i.e. every manager over $100M — plus the
investment-adviser and broker-dealer SIC directories) and from exchange member
directories, where the prop shops are. Each candidate gets its careers page read;
the ones with a live board are promoted into the registry and swept from then on.
794 curated firms is the floor, not the ceiling.

---

## Setup (about 10 minutes, once)

### 1. Create the repo

```bash
gh repo create quant-radar --private --source=. --push
# or: create it on github.com, then
#   git init && git add -A && git commit -m "init" && git remote add origin <url> && git push -u origin main
```

### 2. Turn on Pages

Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.

A private repo needs GitHub Pro for Pages. If you'd rather keep it free, make the
repo public — nothing in it is sensitive except `applications.csv`, which you can
move out (see *Keeping your tracker private* below).

### 3. Alerts

**Nothing to do — alerts work out of the box.** Each sweep that finds new
postings opens an Issue in your own repo, and GitHub emails you about it (and
pushes it to the GitHub phone app). It uses `GITHUB_TOKEN`, which Actions
injects automatically and which is scoped to this repository only. No password
of yours is involved.

Optionally add a repo **Variable** (Settings → Secrets and variables → Actions →
Variables) called `SITE_URL` = `https://<you>.github.io/quant-radar`, so the
issues link back to the board.

<details>
<summary>Optional: proper email digests instead</summary>

Nicer formatting, but it needs a mail credential. Add these **Secrets**:

| Secret | Value |
|---|---|
| `SMTP_USER` | your email address |
| `SMTP_PASS` | a Gmail **app password**, not your account password |
| `MAIL_TO` | where to send it |

Gmail app password: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
(needs 2FA on). Optional: `SMTP_HOST` / `SMTP_PORT` for non-Gmail, `MAIL_FROM`.

Be aware an app password is a real credential to that mailbox. The GitHub Issue
channel avoids that entirely, which is why it's the default. Both can run at once.
</details>

### 4. First run

**Actions → radar → Run workflow**.

Discovery is spread across runs on purpose. Working out which ATS a firm uses
costs ~28 requests when we're guessing, and 794 firms is ~22,000 requests — too
much for one job. So each run probes at most 160 not-yet-known firms and defers
the rest, which means **full coverage arrives after about 5 sweeps, roughly 20
hours.** The board is useful from the first run and keeps filling in.

Once a firm is resolved its board costs one request per sweep, and steady-state
runs take 3–6 minutes. Raise or remove the cap with `--probe-budget N` (0 =
unlimited) if you'd rather take one long run.

Your board is then live at `https://<you>.github.io/quant-radar`.

**That first run deliberately sends no email.** It's a baseline: it records every
posting that already exists so that later sweeps can tell the difference between
"this is new" and "I just hadn't looked yet". The sweep four hours later is the
first one that can alert. See *How it works* below.

---

## How it works

```
firms.yaml ──► resolve ──► sweep ──► classify ──► diff ──► render ──► email
 794 firms    which ATS?   fetch     score &      what's   site/      new only
                           boards    filter       new?     index.html
```

**`resolve`** is the part that makes 794 firms tractable. Most firms run their
board on a handful of hosted platforms (Greenhouse, Lever, Ashby, SmartRecruiters,
Workday, Recruitee, Workable, Personio), all of which expose a public JSON
endpoint. The registry ships *candidate* slugs rather than verified ones, and the
resolver probes the adapter × slug matrix until something returns real postings,
then caches the answer for 30 days. Firms it can't crack are retried weekly and
listed at the bottom of the site for manual checking.

Guessing slugs is the *last* resort, though, because it fails on exactly the
firms you most want. Hudson River Trading's public board is
`boards.greenhouse.io/hrttalentcommunity` — no derivation from "Hudson River
Trading" produces that, but their careers page says it in an href. So before
guessing, the resolver **fetches the firm's own careers page and reads the ATS
URL out of the markup**: Greenhouse embed scripts, Lever/Ashby iframes, Workday
tenant URLs, SmartRecruiters, Recruitee, Workable, Personio. Belvedere Trading
is another one this catches — they're on Lever, not Greenhouse as I'd guessed.

Firms that serve listings straight from their own domain with no JSON behind
them (Citadel Securities, Jane Street, SIG) get the `html:` adapter, which pulls
job links out of the page and is deliberately strict — it returns nothing rather
than filling your board with navigation links.

The slug matrix is deliberately bounded. Cheap adapters get 3 slug spellings each;
Workday gets 1, because a blind Workday probe means POSTing to a tenant × site ×
pod grid — 72 requests for one guess. Capping the blind grid to 12 and the slugs
to 1 took the per-firm discovery cost from ~470 requests to ~28. Without that,
794 firms would be 370,000 requests and the job would never finish.

**`diff`** is what makes the alerts trustworthy, and it is the fiddliest part.

Careers APIs lie about dates. Workday will tell you a posting is "30+ Days Ago"
the hour it goes up; Greenhouse's `updated_at` moves when someone fixes a typo.
So the honest signal for *new* is "we swept 4 hours ago and this wasn't there" —
every posting gets a `first_seen` stamp on the sweep that first caught it, stored
in `data/jobs.json` and committed back to the repo each run.

But `first_seen` alone is wrong in two situations, and both of them happen
constantly:

- **The first ever run.** State is empty, so every posting on earth is "not there
  last time". Jane Street's month-old listing would show up as NEW.
- **A board resolving for the first time.** If the resolver cracks a firm's board
  in week 3, its entire 200-posting backlog arrives at once and all of it looks
  brand new.

So each posting gets a **detection state** the first time it enters our state:

| state | meaning | NEW badge | email |
|---|---|---|---|
| `baseline` | It was already on the board the first time we looked — either our first run ever, or our first successful read of that firm | no | no |
| `backfill` | We genuinely detected it this run, but the board says it has been up more than 21 days. You aren't going to be first to a month-old listing | no | no |
| `live` | We watched it appear, and the board either calls it fresh or gives no date | **yes** | **yes** |

On the site, `seen` is our detection time and `posted` is what the board claims.
A `≥` in front of the seen age means "it was already there when we started
watching, so this is a floor, not the real age". Only `live` postings get a NEW
badge, which is what makes the badge worth anything.

The practical consequence: **your first run is a baseline and emails you nothing.**
It records the market as it already is. The second sweep, four hours later, is the
first one that can tell you something genuinely appeared.

The same state file is why a board going down doesn't spam you: a posting is only
marked closed if its firm's board actually responded that run.

**`classify`** assigns each posting a level (intern / new grad / entry /
experienced), a category (quant trading, quant research, quant dev, portfolio,
S&T, strats & risk, data, software, ops), a language-risk flag, and a score.

It also sets a **`target`** flag, which is the thing the board is actually for:
an internship, summer analyst, summer associate or graduate seat for the **2027
cycle**. Target roles top the ranking, are the default view on the site, and
always trigger an email regardless of score.

An *undated* internship counts as a target. Firms post "Quantitative Trading
Intern" with no year constantly, and it's always the cycle currently recruiting —
excluding them would drop most of the board. Only an explicitly past year
(2026 and earlier) disqualifies, and those get filtered off the board entirely so
stale postings nobody took down don't clutter it.

When the 2028 cycle opens, roll `TARGET_CYCLES` and `PAST_CYCLES` forward in
`radar/classify.py`. That's the only edit needed.

---

## Day-to-day

**The email** only fires for genuinely new postings scoring ≥40 — roughly
"intern or grad seat at a real firm in a role you'd take". Everything else is on
the site.

**The site** defaults to newest-first. `/` focuses the search box; column headers
sort; the chips toggle new-24h-only, hiding language-risk locations, and hiding
things you've applied to.

**Language risk** is flagged, not filtered, as you asked: `lang` means the
location very likely needs Japanese/Mandarin/Korean, `lang?` means a
continental-European or LatAm office where it's a maybe.

### Keeping the applied tracker current

`applications.csv` is pre-filled from what you've already told me. Columns:

```
firm,role,location,status,applied_date,url,notes
```

Rows are matched to live postings by firm name plus a fuzzy title match, so
`Quant Trader Intern Summer 2027` will match a posting called
`Quantitative Trading Internship - Summer 2027`. Matched rows get hidden from the
default view and never trigger an email.

To export from your existing Google Sheet: **File → Download → CSV**, then map
your columns onto those seven headers. If you paste the sheet's real column names
here I'll write the mapping script.

### Keeping your tracker private

If you make the repo public, move the tracker out:

```bash
git rm --cached applications.csv && echo "applications.csv" >> .gitignore
```

then add its contents as a repository secret `APPLICATIONS_CSV` and add this step
to the workflow before *Sweep*:

```yaml
      - run: printf '%s' "${{ secrets.APPLICATIONS_CSV }}" > applications.csv
```

---

## Two workflows

| workflow | when | finds |
|---|---|---|
| `radar` | every 4 hours | **new jobs** at firms already in the registry |
| `discover` | weekly, Mondays | **new firms** to add to the registry |

`discover` runs in two stages. *Harvest* pulls names from SEC EDGAR's 13F filer
index (the richest list of hedge funds and asset managers that exists — every
manager over $100M, in a plain text file), EDGAR's SIC directories for investment
advisers and broker-dealers, and exchange member directories. *Qualify* takes the
best-looking names, guesses a domain, reads the careers page, and promotes
anything with a live board.

Qualification costs ~6 requests per candidate, so it runs on a budget of 120 per
week and works through the backlog. Names are ranked, so "Apex Quantitative
Trading" is checked long before "Generic Holdings", and obvious junk (dental
practices, churches — EDGAR is full of them) is dropped on sight.

Dedupe is fuzzier than exact matching on purpose: EDGAR calls it "JANE STREET
CAPITAL, L.P." and the curated registry says "Jane Street". Exact keys would
treat those as two firms and sweep the same board twice under two names.

Run it by hand any time: **Actions → discover → Run workflow**. Set the
`SEC_USER_AGENT` repo variable to `your-name your@email.com` — SEC blocks
requests without a real contact string.

```bash
python -m radar.main --discover-only -v                      # harvest + qualify
python -m radar.main --discover-only --discover-budget 400   # bigger batch
python -m radar.main --discover-only --discover-sources edgar-13f
```

---

## Adding firms

Append to `firms.yaml`. Leave `ats` empty and the resolver will go find the board:

```yaml
  - name: Some New Fund
    category: fund          # prop | fund | bank | exchange | fintech | crypto
    regions: [uk]
    ats: []                 # or ["greenhouse:somenewfund"] if you know it
    slugs: [somenewfund, some-new-fund]
```

Then **Run workflow** with `force_resolve` ticked (or just wait — unresolved firms
are retried weekly).

If you know a firm's board URL, giving the exact `ats` hint is much faster than
letting it probe. The formats:

```
greenhouse:<slug>          boards.greenhouse.io/<slug>
lever:<slug>               jobs.lever.co/<slug>
ashby:<slug>               jobs.ashbyhq.com/<slug>
smartrecruiters:<slug>     jobs.smartrecruiters.com/<slug>
recruitee:<slug>           <slug>.recruitee.com
workable:<slug>            apply.workable.com/<slug>
personio:<slug>            <slug>.jobs.personio.de
workday:<tenant>           probes wd1..wd103 and ~12 common site names
workday:<tenant>/<site>    exact, much faster
custom:<url>               any URL returning a JSON array of postings
```

---

## Running locally

```bash
pip install -r requirements.txt

python -m radar.main --limit-firms 20 -v        # quick smoke test
python -m radar.main --resolve-only             # just work out the ATS map
python -m radar.main --no-email                 # full sweep, no email
python -m radar.main --dry-run-email            # print the digest instead of sending
python -m radar.main --include-experienced      # keep senior roles too

python tests/test_pipeline.py                   # 50-check offline test suite
```

Output lands in `site/index.html` — open it directly in a browser.

---

## Tuning what reaches you

| What | Where |
|---|---|
| Email threshold (default score ≥ 40) | `radar/main.py`, `alertable` |
| "Too old to be news" cutoff (21 days) | `radar/store.py`, `BACKFILL_AFTER_DAYS` |
| Target cycle (2027) | `radar/classify.py`, `TARGET_CYCLES` / `PAST_CYCLES` |
| Role keywords | `radar/classify.py`, `CATEGORY_RULES` |
| Junk filter | `radar/classify.py`, `EXCLUDE_PAT` |
| Language-risk cities | `radar/classify.py`, `HIGH_RISK_LOCS` / `MED_RISK_LOCS` |
| Ranking weights | `radar/classify.py`, `*_WEIGHT` |
| Sweep frequency | `.github/workflows/radar.yml`, the cron line |

A note on the regexes in `classify.py`: they are **stem** patterns with a leading
`\b` and deliberately no trailing `\b`. `data scien` has to match "Data Scientist"
and `trad` has to match both "Trading" and "Trader". Adding a closing boundary
silently breaks them — that bug was in the first version of this file and the test
suite is what caught it.

---

## Honest limits

- **"Every firm in existence" isn't a thing you can finish.** 794 is a broad
  list, not a complete one — it covers the majors, the regional prop shops, the
  CTAs, the Chinese and Indian quant funds, the pod shops, market infra and
  crypto desks. Adding a firm is two lines of YAML, and the site's bottom panel
  tells you which boards the resolver couldn't read.
- **Registry entries are unverified by design.** I wrote the firm list from
  knowledge, not by checking 794 websites, so some names will be slightly off,
  merged into a parent, or defunct. Those resolve to nothing and land in the
  manual-check panel — cheap to spot, cheap to delete. The alternative, shipping
  only firms I could verify from here, would have been a much shorter list.
- **Some firms will never resolve.** Jane Street, SIG, Optiver and a few others
  run bespoke boards with no JSON endpoint. Those land in the manual-check panel
  with a search link. If you want any of them scraped properly, say which and
  I'll write a dedicated adapter.
- **4 hours is GitHub's practical floor for a free scheduled job**, and the
  scheduler drifts by 5–15 minutes under load. If you want tighter than that for
  a shortlist of ~20 firms, a second workflow on a 30-minute cron over just those
  is cheap — ask and I'll add it.
- **Classification is heuristic.** It will occasionally file a posting under the
  wrong category. It's tuned to be over-inclusive rather than miss things, which
  is the right error for what you're using it for.
