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
a **run-wide** allowance shared by every company, not 40 per company.

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
an automatic pass: per LinkedIn analysis, ~35% of postings labelled
"entry-level" still demand 3+ years.

**Commands:**

```
/filter                    state of every filter
/filter experience on|off  turn one on or off
/minexp 2                  change the threshold (in years)
/minexp strict on|off      in strict mode, "undetermined" is suppressed
                           instead of sent
/stats                     counters: rejected by title / number found and
                           passed / number found and rejected /
                           undetermined / undetermined with seniority signals
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

**142 companies are registered** as of 2026-08-13, up from 3. All 142 load
with no profile errors, and a full live fetch returns **1,373 Israel-relevant
postings in 39.6s** (142/142 succeeded).

| Platform | Companies | Shape |
|---|---:|---|
| Comeet | 104 | thin records over `_platforms/comeet.json` |
| Greenhouse | 28 | thin records over `_platforms/greenhouse.json` |
| Lever | 7 | thin records (1 EU-hosted) |
| Ashby | 2 | thin records |
| *(standalone)* | 1 | `wix` — the only `playwright` company |

139 of these were bulk-imported by `_onboarding/import_companies.py` from a
152-row shortlist that was **live-verified first** (see
`_onboarding/verify_report.md`): 10 identifiers were dead and skipped, and
BioCatch's second, abandoned board was dropped by an explicit decision. The
importer is idempotent and re-runnable.

**All 142 are seeded.** Done in six batches of 25 via `--seed --limit 25`,
1,371 postings recorded as already-known, no alerts sent. A simulated run
immediately afterwards reported **0 seed gaps, 0 fetch failures, 0 health-gate
trips, 0 companies below their floor, and 1 new job** (ordinary churn).

**⚠️ The scheduled workflow has not been turned on for this set.** The cron in
`check.yml` dates from the 3-company era and only fires from the default
branch, so **merging this branch is what makes it live.**

**A relevance-filter leak was found and fixed while verifying the seed.**
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
- **`wix` is still `schema_version: 2` with no `detail_fetch`**, so the
  filter only works at the title level there and everything else is sent with
  `⚠️`. That is correct fail-open behaviour, not a bug. Determining how to
  reach a posting's description there needs a `career-site-profiler` session
  (the listing is `playwright`, and the posting page itself has not been
  examined).
