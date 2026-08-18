# job-alert-agent

A Telegram bot that watches companies' careers pages and alerts on new job
postings (Israel-relevant, including qualified remote). Runs entirely on
GitHub Actions - there is no server.

## How it works

- **A profile IS the registration.** Every company is one JSON file, in the
  structure defined by `career-site-profiler`. There is no separate
  `companies.json`. A company can be written two ways, and they behave
  identically because the second resolves into the first at load time:
  - `profiles/<slug>.json` — a full standalone profile. The right answer for a
    company that doesn't fit any platform's shape (`wix`).
  - `profiles/companies/<slug>.json` — a thin record naming a `platform` plus
    only what differs for that company (endpoint, health numbers). It is merged
    over `profiles/_platforms/<platform>.json`, which holds everything
    identical across every customer of that ATS, and the merged document is
    then validated exactly like a standalone profile.

  `platform` selects a **file**, nothing more. The fetch dispatch still reads
  `fetch_type` and `api.platform` off the *resolved* document, so there is no
  platform-name-to-function map anywhere in the resolution path, and a company
  may override any inherited field.
- **The dispatcher** (`src/fetchers/__init__.py`) reads `fetch_type` from the
  profile and routes to the matching module (`api.py` / `html.py` /
  `browser.py`), which in turn read further fields (`platform`,
  `pagination.method`, `israel_filter.structure`) to pick a strategy. A new
  company on an existing combination = one new JSON file, zero code.
- **"New job" detection** always runs on `Job.id` (the ATS's own id, or a
  canonicalized link) and never on display text - so a cosmetic change on the
  company's side can't produce a duplicate alert.
- **Health gate**, three thresholds. State is not overwritten and a
  maintenance alert goes out after two consecutive failures. A fetch that
  *raises* is counted the same way (`state.record_failure`) - it reaches the
  same threshold as a suspicious zero, rather than being logged and forgotten.
  See "Health gate" below for the three and why they don't overlap.
- **A profile is validated at load, not at fetch.** Anything the fetchers
  actually read is checked up front - the API platform must have a handler, a
  `ui_interaction` filter must carry `ui_actions_structured`, a
  `flat_multi_location` one must carry the selectors it walks. The failure
  these prevent is the quiet kind: a profile that loads, runs, raises nothing,
  and returns the wrong set of jobs.
- **Relevance = Israel, or remote that Israel can actually reach**
  (`src/relevance.py`, kept in sync with the skill's `RELEVANCE_HELPER`).
  `Remote`, `Remote - EMEA`, `Remote - Europe`, `Remote - Global` are kept;
  anything naming a foreign country **or city** is not - `Remote - New York`,
  `Hybrid - Boston`, `Remote - Zurich`. The city half of that list matters
  only at scale: with three companies those postings barely appear, with a
  hundred they are a steady trickle of jobs nobody here can take.
  A **qualified-remote posting also has its title checked** against the same
  marker list, because the region is routinely written there instead -
  `Technical Account Manager - UK` at location `Remote`. The title is consulted
  on that path only: a role with a physical Israeli location is relevant
  whatever its title says, or `Sales Engineer, DACH - Tel Aviv` would be
  dropped for naming the market it serves.
- **The fetch phase - and only the fetch phase - runs concurrently**
  (`fetchers.fetch_all`). Everything after it stays sequential and in profile
  order. See below.
- **A filter chain** (`src/filters.py`) runs between the diff and the send,
  deciding what is *shown* - not what counts as new. See below.

## Concurrency and run time

A run's cost is almost entirely the fetch loop, and every company is an
independent call to a different host - so the fetch runs in two pools, sized
by the resource each kind of fetch actually consumes:

| Pool | Companies | Default | Bounded by |
|---|---|---|---|
| `JOB_ALERT_NETWORK_WORKERS` | `api`, `html` | 12 | remote-host politeness; each is a different host |
| `JOB_ALERT_BROWSER_WORKERS` | `playwright` | 2 | RAM/CPU - one whole Chromium each |

Measured per company: **api ~0.4-1s, playwright ~30s**. That ~30x gap is why
the two are pooled separately, and why the browser count is the one that
decides a run's wall clock.

**The browser bound is a correctness limit, not a performance knob.**
Oversubscribing it doesn't just run slower - contention pushes the
first-page selector wait past its budget. Measured on the live Wix page: 3
concurrent Chromiums on a 4-core laptop turned a healthy 16-job fetch into
**0 jobs on all three**. Raise it only together with a bigger runner
(GitHub-hosted is 2 vCPU / 7 GB on a private repo, 4 / 16 on a public one).

**Everything downstream of the fetch stays sequential**, in profile order:
the diff, the state write, the filter chain's shared counters and the
Telegram send are order- or rate-sensitive, and cost little enough that
parallelising them would buy noise and risk. Results are consumed in profile
order however the pool scheduled them, so logs, state writes and alert order
stay deterministic.

Rough wall clock at 100 companies, dominated by the browser count:

| Mix (api/html/playwright) | Sequential | Concurrent |
|---|---|---|
| 80 / 15 / 5 | ~7 min | **~2 min** |
| 60 / 15 / 25 | ~15 min | **~4 min** |
| 40 / 20 / 40 | ~24 min (over the 20-min timeout) | **~6 min** |

Fixed overhead is ~2 min regardless of company count (checkout, pip install,
`playwright install --with-deps`, the state commit).

### Time budgets

The estimates above are estimates. A slow site, a cold CDN or a retry can
double a company's time, so there are two hard budgets, both sized against
check.yml's `timeout-minutes: 20` rather than against anything in the code:

| Budget | Default | What it does |
|---|---|---|
| `JOB_ALERT_FETCH_BUDGET_SECONDS` | 780 (13 min) | Once reached, companies whose fetch hasn't *started* are skipped for this run. Not failures - nothing was tried, so no counter bump and no maintenance alert. |
| `JOB_ALERT_COMPANY_BUDGET_SECONDS` | 240 | Wall-clock ceiling on one company's paginated walk (`pagination.max_seconds` overrides per profile). Exceeding it **raises** - a partial page walk would be indistinguishable from a company with fewer jobs. |

**Why a budget at all**: the workflow kills the job at 20 minutes, and a
killed job never reaches the "Commit updated state" step - so every state
write the run had already made is thrown away, and the next run repeats the
same work into the same wall. Finishing early with some companies skipped is
strictly better than finishing none of them. The company budget exists
because the fetch budget can only stop companies that haven't *started*: a
thread holding a Chromium can't be interrupted.

The detail layer has a matching budget: `MAX_DETAIL_FETCHES_PER_RUN` (40) is
a **run-wide** allowance shared by every company, not 40 per company. As of
2026-08-18 it is almost never reached: **98% of postings now carry their
description inline** in the listing response, so they cost no request at all.
Only `wix` (schema v2, no `detail_fetch`) and any future HTML-detail profile
draw on it. That headroom is what makes a much larger company count viable -
the cap, not the fetch time, was the binding constraint.

**Telegram sends are paced** at ~1/second (`JOB_ALERT_SEND_INTERVAL`) and a
429 is retried after the `retry_after` Telegram asks for. Every company sends
to the same chat, so at three companies this never fires and at a hundred it
is the likeliest thing to break.

**Actions minutes**: only billed on a *private* repo. At 8 runs/day that is
240 runs/month, so a 2,000-minute free tier affords ~8 min per run - which
sequential fetching at 100 companies would blow, and concurrent fetching
comfortably fits. On a public repo this cost does not exist.

## Health gate

The worst failure this project has is not a crash - it is the scraper quietly
returning *fewer* jobs than exist. The diff only reports what it saw; what it
didn't see is never mentioned, nothing raises, and the bot goes silent while
looking perfectly healthy. Three checks, covering three different shapes of
that (all skipped when `zero_is_plausible`):

| Check | Catches | Needs |
|---|---|---|
| count is **0** after a healthy run | a dead `job_selector` | nothing |
| count below **`health.expected_min_jobs`**, from a run above it | **slow decay** - a drift downwards over weeks never trips a run-to-run comparison | a number in the profile |
| count below **40% of the last healthy run** (baseline ≥ 10) | **sudden breakage** - broken pagination, page 1 parses and pages 2..N stop coming | nothing |

The last two overlap on a good day and cover opposite failure shapes on a bad
one, which is why both are there. **The ratio is the one that scales**:
`expected_min_jobs` is a number a human picked on one day, and at a hundred
companies those numbers go stale faster than anyone maintains them. Set it
lazily - roughly half the observed count is fine. It only has to catch the
decay the ratio can't see.

**Partial collapses time out; a total zero doesn't.** While the gate holds,
`process_company` returns no new jobs - so a *false* positive on a partial
collapse silently stops detecting real postings at that company, which is the
very failure the gate exists to prevent. After
`PARTIAL_COLLAPSE_ACCEPT_AFTER` (3) consecutive runs it reports the drop one
last time, accepts the lower count as the new normal, and resumes detecting.
A total zero has no such cost - there are no jobs to miss - so it stays
frozen until a human looks.

## Experience filter

Default: **ON**, suppressing postings that require **more than one year** of
experience.

The experience requirement is not in the job listing (the listing returns
only `id`/`title`/`location`/`url`), so there is a detail layer
(`src/detail.py`) that fetches the posting's description according to
`detail_fetch` in the profile - `inline` (the description already arrived
with the listing, zero requests), `html`, `playwright`, or `none`.

**Always check for `inline` before building a per-posting request.** This is
the project's cheap-to-expensive rule, and it has paid off: both Lever and
Greenhouse return the full description in the listing itself, so both
verified companies cost zero extra requests. `inline_field` also supports a
field that is an **array of sections**
(`[{text: heading, content: "<li>..."}]`, Lever's shape) and not just a
string - `detail.render_sections` turns it into HTML before parsing, keeping
the headings, because the heading is what promotes an unmarked bullet to
"mandatory".

**Order of operations - critical:**

```
fetch → dedupe → diff against state → NEW jobs
      → write ALL new ids to state        ← BEFORE filtering
      → title pre-check → detail fetch → filter chain
      → notify survivors
```

State is written **before** filtering on purpose: state means "everything
ever seen", filtering is presentation only. If suppressed jobs were left out
of state, each one would look new on every run and its detail page would be
re-fetched forever. **Accepted consequence:** turning the filter off does not
retroactively deliver jobs suppressed while it was on. There is no replay
mechanism, by design.

**What happens when a send fails.** That same ordering means a company whose
alert failed has jobs recorded as "seen" that the user never saw, and
committing that would make the loss permanent. So the run **rewinds that one
company** to its pre-run state - the jobs are un-seen and re-sent next run -
and every other company still commits. Only if the rewind itself fails does
the run exit non-zero, which skips the commit step and discards everything.

That fallback used to be the only behaviour, and it had a cost that only
appears at scale: discarding every company's state meant one broken company
re-sent *all* the others' alerts on the next run, every run, until it was
fixed.

**Fail-open**: if no number is found, the posting is **sent**, flagged.
Losing a relevant job costs far more than two seconds of scrolling. A
detail-layer failure (404, dead selector, timeout) is likewise just
"undetermined" - it does **not** mark the company as failing, does not block
the state write, and does not abort the run. That is entirely separate from
`health` / `expected_min_jobs`, which concern the listing and were left
untouched.

**Three flag levels** (all three are sent - the flag only sorts them):

| Flag (as sent, in Hebrew) | Meaning |
|---|---|
| `✅ ניסיון: עד שנה` | A number was found, and it is at or below the threshold |
| `⚠️ לא צוינה דרישת ניסיון` | No number found at all |
| `🔶 לא צוין מספר, יש סימני ותק` | No number, but `proven experience` / an advanced-degree requirement is present |

The third level exists because, per Indeed Hiring Lab data (US, April 2024),
only ~30% of postings state a number of years - meaning "undetermined" is the
majority, and without a further split the flag would be meaningless.

**The title pre-check** rejects `senior`/`lead`/`staff`/`principal`/`manager`
and friends at zero request cost. A junior-sounding title does **not** earn
an automatic pass on the *experience* question: per LinkedIn analysis, ~35% of
postings labelled "entry-level" still demand 3+ years, so they are still
fetched and still evaluated on their stated number.

What a junior word *does* do (added 2026-08-18) is stop a seniority noun
elsewhere in the same title from rejecting the posting outright. The only
false negative in the bot's entire delivered history was mprest's **`Junior
Project Manager`**, rejected for containing "manager". Also added that day,
with the count each caught on a live snapshot: `leader` (33 - `lead` is
whole-word, so it never matched `Leader`), `experienced` (20), `expert` (15),
and the C-level set. Deliberately *not* added: `specialist` (30 postings, and
it is a level rather than a seniority - `Technical Support Specialist -
Student Position`) and `owner` (`Product Owner` is routinely a mid role).
`(CTO Group)` is exempted - at Mobileye that names an organisation, not a
level.

## Role filter

Default: **ON**. Suppresses postings outside the user's job families, from the
title alone - so it costs no request, and it runs **first** in the chain, which
means an off-target posting never reaches the experience filter's detail fetch
either.

The four target families are software/data/ML engineering, hardware/VLSI/
embedded, data analyst/BI/product, and IT/technical support. Everything else -
finance/legal, sales/CS/marketing, HR/admin, manual and warehouse - is
rejected. Measured before it existed: **~47% of delivered alerts were outside
these families** (Bookkeeper, Payroll Accountant, General Counsel, Securities
Sales Representative, Warehouse Clerk, Marketing Admin).

**It is a blocklist, not an allowlist, and that is not an accident.** The
corpus contains real on-target roles whose titles name no technology at all -
`DFIR`, `CyOps Analyst`, `InfoSec & SecOps`, `Junior Intelligence Analyst`,
`System Integrator`. An allowlist drops every one of them, and state is written
before filtering, so a drop is permanent. So: blocked domain → reject, target
family → pass, **neither → pass, flagged `❓ תפקיד לא מזוהה`** (~15% of
postings). `/filter role off` disables it; `send_unknown: false` in
`state/filters.json` inverts the fail-open, and is the role filter's equivalent
of `strict` - off by default for the same reason.

`roles.TECH_OVERRIDES` is what lets both of these be right at once: a bare
`engineer` is deliberately **absent** from it, so `Sales Engineer` stays
blocked while `Support Engineer` does not. Matching is whole-word throughout -
`Salesforce Developer` must not read as sales.

**Repeat titles are tagged, never dropped** (`🔁 כותרת שכבר הופיעה`). A posting
that is closed and re-opened comes back with a NEW id - a new Comeet uid, or a
new Wix URL slug, which is what Wix's id is derived from - so the diff
correctly sees a brand-new job and alerts again. Two of the 24 alerts delivered
between 13 and 18 Aug were this (Kaltura `Help Desk`, Wix `Payroll
Accountant`); each company had exactly one live posting with that title, so it
was **not** the per-city duplication `collapse_duplicate_titles` handles.

`filters.recently_seen_titles` reads the company's **pre-run** state snapshot -
`jobs` already records every posting's title and `first_seen`, so no new
storage was needed - and marks any title seen in the last
`REPEAT_TITLE_WINDOW_DAYS` (14). It must be the pre-run snapshot: the state
write happens before filtering, so the live file already contains the new job
and every title would match itself.

Tagging rather than suppressing is a deliberate call. `Help Desk` and `QA
Engineer` are exactly the titles a company reuses for a genuine second req, and
a drop would be permanent - there is no replay. The tag costs one skimmable
line; a wrong suppression costs a job.

Temporary and maternity-cover roles are **tagged, never dropped**
(`⏳ משרה זמנית/חלופת לידה`): Wix's `QA Engineer (Temp position)` and Playtika's
`UI/UX Designer - maternity leave replacement` are real entry points.

**Commands:**

```
/filter                    state of every filter
/filter role on|off        turn the role filter on or off
/filter experience on|off  turn one on or off
/minexp 2                  change the threshold (in years)
/minexp strict on|off      in strict mode, "undetermined" is suppressed
                           instead of sent
/stats                     per-filter counters, each in its own vocabulary:
                           experience - rejected by title / number found and
                           passed / number found and rejected / undetermined /
                           undetermined with seniority signals
                           role - in-scope / off-target / unclassified sent /
                           unclassified dropped
```

Settings are stored in `state/filters.json`, which the workflow already
commits - so they can be changed from a phone with no commit and without
touching the workflow. The `/stats` counters are the only way to tell whether
the filter genuinely works or merely appears to: the ~30% figure is US
aggregate data and may not hold for Israeli postings.

## Adding a company

1. Open a conversation with Claude and ask to add the company - this invokes
   the `career-site-profiler` skill. That skill is also what determines the
   company's `detail_fetch` (verifying it against a real posting URL) - it is
   never guessed by hand.
2. Save the resulting `profile.json`. If the company sits on an ATS that
   already has `profiles/_platforms/<platform>.json`, write a thin record to
   `profiles/companies/<slug>.json` instead — `platform`, `slug`, `name`,
   `careers_url`, `api.endpoint` and `health`, and let the rest be inherited.
   Otherwise save the full profile as `profiles/<slug>.json`.
3. Run `python run.py --seed` (manually, via `workflow_dispatch` or locally)
   to seed state without firing alerts for every already-open posting.
   **`--seed` only touches companies that have no state yet**, so it is safe
   to run after adding profiles in batches: the existing companies cost no
   fetch and keep their `first_seen` history. `--seed --force` re-seeds
   everything, which resets that history - rarely what you want.
   If the fetch budget runs out mid-seed, the companies that were reached are
   committed; just run `--seed` again for the rest.

   For a large batch, seed in **stages** rather than all at once:

   ```bash
   python run.py --seed --limit 25
   ```

   Re-run it to take the next 25 — because the command skips what is already
   seeded, repeated runs walk the backlog without repeating a company, and the
   order is stable so the batches are reproducible. `--only <file>` seeds a
   named list of slugs instead (one per line, `#` comments allowed). Staging
   matters because seeding is the one irreversible step in adding a company: it
   decides which postings count as already-known and are therefore never
   alerted, so doing 139 in one pass records that decision for ~1,350 postings
   with no chance to look in between.
4. From the next cron run, the company is under normal monitoring.

**`/add` in Telegram does NOT do this automatically (deliberate in v1)** - it
only reports that a manual profiling session is needed. `/remove <slug>`
disables a company (`enabled: false`) without deleting history. `/list` shows
every company and its status (names only, not jobs). `/jobs <slug>` fetches
live (not from state) the currently open Israel-relevant postings for that
company - up to 20 in one message (Telegram's 4096-character limit), with a
link to the full careers page if there are more.

**Telegram commands do not run in the background.** No process is listening -
they are read once per cron run (piggybacked), so response time is up to one
cron interval (3 hours) plus GitHub's usual delay. This is a deliberate
design decision: a dedicated cron for commands would on its own burn a
private repo's entire Actions quota.

## Running locally

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # only if a playwright company exists
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export JOB_ALERT_BROWSER_WORKERS=2   # optional - see "Concurrency and run time"
export JOB_ALERT_SEND_INTERVAL=0     # optional - disable send pacing locally
cd src
python run.py --seed           # seeds only companies with no state yet
python run.py --seed --force   # re-seed everything (resets first_seen)
python run.py                  # a normal run
```

## Tests

```bash
cd tests && python -m pytest -v
```

## Current status

**233 companies are registered** as of 2026-08-18, up from 145. All 233 load
with no profile errors, and a full live fetch returns **1,919 Israel-relevant
postings**, of which **1,246 are in a target job family** (233/233 fetched; one
transient `ReadTimeout` on `media_force`, which returns 2 jobs on retry).

| Platform | Companies | Shape |
|---|---:|---|
| Comeet | 108 | thin records over `_platforms/comeet.json` |
| Greenhouse | 83 | thin records over `_platforms/greenhouse.json` |
| Ashby | 24 | thin records |
| Lever | 16 | thin records (1 EU-hosted) |
| HiBob | 1 | its own careers product — see below |
| *(standalone)* | 1 | `wix` — the only `playwright` company |

### The 2026-08-18 import: 88 companies from the discovery sweep

`_onboarding/discover_ats.py discover` was run over **all 7,801 unprofiled rows**
of `israeli_companies_seed.csv` — 5 hours, ~288,000 requests, one HTTP 429 in
total. It found **342 reachable boards (4.4%)**, of which **117 have at least one
Israel-relevant posting in a target job family**; the other 225 are structurally
dead (175 have open postings but none in Israel, 39 have empty boards, 11 are
Israeli but entirely off-family) and were not imported.

Of the 117, **88 were imported** — those on a platform that already has both a
`_platforms/` profile and a fetcher, so each is one JSON file and zero code. The
remaining 29 are deferred and listed below.

- **Yield is heavily size-dependent**, and size is not the proxy it looks like.
  Per 100 companies probed, on-family postings found: `l` **86**, `xl` **54**,
  `m` **15**, `s` **4**, `xs` **0.7**. The `xs` bucket cost 4,138 probes for 29
  postings — worth running once to establish that, not worth running again.
- **The seed CSV is now exhausted.** Growing past 233 is a sourcing problem, not
  a scraping one: another pass over the same file cannot help.
- The new companies are the same calibre as the existing ones — they add **391
  on-family postings across 88 companies**, against 856 across the original 145.
- **A caveat on the sweep's own numbers.** `discover` predicted +606 on-family
  and the real figure after import is +391. The probe's Israel test is looser
  than the fetcher's: `probe_greenhouse` joins `location.name` and `offices[]`
  into one string, while the fetcher consults `offices[]` only when
  `location.name` carries no place (the defect fixed on 2026-08-18, see below).
  So sweep counts over-report Greenhouse boards and should be read as a ranking
  signal, not as a forecast.

**29 candidates were found but NOT imported**, all for stated reasons:

| Reason | Count | Notes |
|---|---:|---|
| No fetcher for the platform | 11 | Workday — the biggest gap, 69 on-family postings incl. Palo Alto Networks, CrowdStrike Israel R&D, MKS Instruments |
| Identity `unverifiable` | 14 | the platform publishes no company name, so nothing proved the board is theirs. **Lemonade** is the significant one (24 on-family, Ashby); the other 13 are small SmartRecruiters boards worth 1–5 each |
| No fetcher for the platform | 3 | Recruitee ×2, Workable ×1 — 4 on-family postings between them, not worth an adapter |
| No `_platforms/` profile | 1 | SmartRecruiters — `fetch_smartrecruiters` exists in `api.py` but no platform profile does |

**All 88 are seeded**, in four batches of 25 via `--seed --limit 25`, recording
600 postings as already-known. A follow-up `--seed` reports no gap, and the
existing 145 companies' state was not touched — their `first_seen` history is
intact.

139 of these were bulk-imported by `_onboarding/import_companies.py` from a
152-row shortlist that was **live-verified first** (see
`_onboarding/verify_report.md`): 10 identifiers were dead and skipped, and
BioCatch's second, abandoned board was dropped by an explicit decision. The
importer is idempotent and re-runnable.

**All 145 are seeded.** The first 142 in six batches of 25 via
`--seed --limit 25`; the last three when they were added. A simulated run
afterwards reported **0 seed gaps, 0 fetch failures, 0 health-gate trips, and
2 new jobs** (ordinary churn).

**The 10 dead identifiers from the shortlist were re-resolved** — 3 recovered,
7 deliberately left out. Full findings in
`_onboarding/dead_rows_reresolution.md`:

- **Viz.ai** → Ashby, slug `Viz.ai` (capital V, literal dot — no lowercase
  guess would have found it). Listed as *Greenhouse* in the shortlist.
- **Insightec** → Comeet uid `4A.004`. Also listed as Greenhouse. So for two of
  the three, the shortlist's *platform* was wrong, not just the identifier.
- **HiBob** → its own careers product, 17 Israel-relevant roles. Needed a new
  handler; only a browser found it, because the page source still carries
  leftover Comeet CSS class names that read as a false positive.
- The other 7 moved to Workday (Digital Turbine, NeoGames-via-Aristocrat),
  RippleHire (CyberProof) or BambooHR (Cyberbit) — none implemented here — or
  self-host with almost nothing open (Deep Instinct 0 Israeli roles, MASSIVit
  1 non-R&D, REE 1).

This also corrected a **wrong claim in the Phase 1 report**: the 82,866-byte
Comeet "shell" page was cited as proof of a closed account, but a known-good
uid with a *wrong slug* returns the same page. The rows were still correctly
excluded, but the stated evidence was stronger than it was.

**⚠️ The scheduled workflow has not been turned on for this set.** The cron in
`check.yml` dates from the 3-company era and only fires from the default
branch, so **merging this branch is what makes it live.**

**A second `offices[]` defect was fixed on 2026-08-18.** The fetcher treated
`offices[]` as an OR against `location.name`, so any posting on a board whose
regional office list happens to include Tel Aviv was kept regardless of where
the role actually is. Datadog attaches
`[Bordeaux, Lisbon, Lyon, Madrid, ..., Tel Aviv]` to roles located in Paris and
Madrid: 13 of them leaked, 21 across the corpus. `offices[]` is now consulted
**only when `location.name` carries no place** (empty, `Multiple Locations`),
which is what the platform profile always said it was for.

**An earlier relevance-filter leak was found and fixed while verifying the seed.**
Greenhouse publishes a separate `offices[]` array whose entries the fetcher
checks individually, and office names are bare sub-national remote regions with
no country token — `Remote - Colorado`, `Remote - Texas`. Checked alone those
hit the remote keyword, matched no foreign marker, and were kept as qualified
remote: exactly the `Remote-US` case `relevance.py` says it excludes. 31
distinct office strings were leaking, 68 jobs at Datadog alone. All 50 US
states and the Canadian provinces are now markers, and the 5 companies whose
counts changed were re-seeded. Invisible at 3 companies because neither Lever
nor the Wix page produces that shape of string.

- **14 companies carry `zero_is_plausible: true`** because a live check found
  their board reachable and non-empty but with no Israel-relevant postings.
  Without that flag each would fire a false maintenance alert on its first run.
- `mobileye` - API profile (Lever), **live-verified** on 2026-08-11, 2026-08-12
  and 2026-08-13: 117-122 Israel-relevant postings (`expected_min_jobs: 20`,
  `zero_is_plausible: false`). State is seeded (`state/seen/`), no seed gap.
  Migrated to a thin record on 2026-08-13; its hand-chosen health numbers and
  its EU API host are preserved as per-company overrides.
- `wiz` - API profile (Greenhouse), **live-verified** on 2026-08-12 (the
  earlier profile note claiming the endpoint was unreachable was stale - this
  environment's egress was blocked on 2026-08-11 and is open now). The board
  has exactly 2 open jobs company-wide, both outside Israel, so it returns 0
  Israel-relevant postings and `zero_is_plausible: true` remains correct.
  State is seeded, no seed gap.
- `wix` - `playwright` profile (no known ATS; the careers page is built on
  Wix's own platform), **live-verified** on 2026-08-11 two ways: a DOM/network
  inspection in the browser, and a real `fetch()` run returning 15 Tel Aviv
  postings (`expected_min_jobs: 6`, `zero_is_plausible: false`). The Israel
  filter runs `post_fetch` over the full list rather than the site's location
  picker, which breaks when filtering two cities at once - see
  `israel_filter._note` in the profile. Re-verified on 2026-08-12: selectors
  intact, 16 then 17 Israel-relevant postings an hour apart (ordinary churn,
  not drift - the profile was not rebuilt). **Seed gap closed on 2026-08-12**:
  `state/seen/wix.json` holds 17 ids. Seeded wix alone rather than via a full
  `run.py --seed`, so mobileye and wiz kept their existing state instead of
  having every `first_seen` reset to the seed timestamp.
  - **Fixed on 2026-08-12, found while sizing the fetch pools:** a first run
    returned **0 jobs without raising**. Not a dead selector - the
    `comp-*` ids are all still present. The cards on this page land 4-9s
    after `load` fires, and `_fetch_url_pages` gave *every* page the same
    flat 10s selector budget while treating expiry as "no more pages". A
    merely slow first page therefore returned an empty list that is
    indistinguishable from "no open roles", and the health gate then reports
    it as a broken selector. Page 1 now gets its own budget (30s, profile-
    overridable via `pagination.first_page_timeout_ms`), one retry, and then
    raises `ListingNeverRendered` instead of returning nothing. This is a
    prerequisite for concurrency, not a side quest: under 3 concurrent
    browsers the old code returned 0 jobs *every* time.

### Experience filter status

- **`mobileye` and `wiz` are on `schema_version: 3`** with `detail_fetch`
  **live-verified on 2026-08-12**, both `method: inline` (zero extra
  requests):
  - `mobileye` → `inline_field: lists`. Lever's listing already contains the
    entire description, so `method: html` is unnecessary. The field was
    chosen by testing each candidate against the real parser over 25 live
    postings: `description`/`descriptionBody` hold only the intro paragraph
    and yielded a number on 0 of 25, while `lists` yielded one on 18.
  - `wiz` → `inline_field: content` with `content_is_html: true` (the field
    arrives as HTML **and** HTML-escaped - verified live). This is the only
    viable path there: a Greenhouse posting page serves only the application
    form.
- **Live result on Mobileye:** of 122 Israel-relevant postings, 5 pass the
  filter (95% suppressed) - 63 rejected by title, 54 on an explicitly stated
  number of years. The determination rate is **91%**, against the ~30% US
  baseline the design was planned around: Israeli tech postings state a
  number far more often than the US average.
- Note: no Mobileye posting requires one year or less (the lowest found is
  two years, in 6 postings), which is why `passed_with_number` is 0.
  `/minexp 2` would open up those 6.
- **Comeet (105 companies) uses `detail_fetch.method: inline`** — changed
  2026-08-18, from the `embedded_json` page scrape added 2026-08-13. The
  listing endpoint *does* serve every description; it just needs
  `&details=true`, which `api.extra_params` now adds. Verified live against
  all 105 boards: 105/105 returned a populated `details` array, the same
  `{name, value}` section shape the page scrape was reaching.
  The scrape worked but cost **one GET and ~1.06s per new posting** against the
  run-wide cap of 40; this costs **zero extra requests** and is uncapped, for
  57 KB → 244 KB on the one listing call. That is the change that unblocks
  scaling the company count: a single company reworking its board used to spend
  the entire run's detail allowance, leaving every later posting `undetermined`
  and delivered untriaged.
- **`wix` is still `schema_version: 2` with no `detail_fetch`**, so the
  filter only works at the title level there and everything else is sent with
  `⚠️`. That is correct fail-open behaviour, not a bug. Determining how to
  reach a posting's description there needs a `career-site-profiler` session
  (the listing is `playwright`, and the posting page itself has not been
  examined).
