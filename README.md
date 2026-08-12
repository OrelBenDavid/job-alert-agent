# job-alert-agent

A Telegram bot that watches companies' careers pages and alerts on new job
postings (Israel-relevant, including qualified remote). Runs entirely on
GitHub Actions - there is no server.

## How it works

- **A profile IS the registration.** Every company is one
  `profiles/<slug>.json` file, in the full structure defined by
  `career-site-profiler`. There is no separate `companies.json`.
- **The dispatcher** (`src/fetchers/__init__.py`) reads `fetch_type` from the
  profile and routes to the matching module (`api.py` / `html.py` /
  `browser.py`), which in turn read further fields (`platform`,
  `pagination.method`, `israel_filter.structure`) to pick a strategy. A new
  company on an existing combination = one new JSON file, zero code.
- **"New job" detection** always runs on `Job.id` (the ATS's own id, or a
  canonicalized link) and never on display text - so a cosmetic change on the
  company's side can't produce a duplicate alert.
- **Health gate**: if a company that used to return jobs suddenly returns 0,
  that counts as a failure (a broken selector), not "no jobs". State is not
  overwritten, and a maintenance alert goes out after two consecutive
  failures. A fetch that *raises* is counted the same way
  (`state.record_failure`) - it reaches the same threshold as a suspicious
  zero, rather than being logged and forgotten.
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

**Actions minutes**: only billed on a *private* repo. At 8 runs/day that is
240 runs/month, so a 2,000-minute free tier affords ~8 min per run - which
sequential fetching at 100 companies would blow, and concurrent fetching
comfortably fits. On a public repo this cost does not exist.

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
2. Save the resulting `profile.json` as `profiles/<slug>.json`.
3. Run `python run.py --seed` (manually, via `workflow_dispatch` or locally)
   to seed state without firing alerts for every already-open posting.
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
cd src
python run.py --seed   # the first time
python run.py          # a normal run
```

## Tests

```bash
cd tests && python -m pytest -v
```

## Current status

- `mobileye` - API profile (Lever), **live-verified** on 2026-08-11 and again
  on 2026-08-12: 122 Israel-relevant postings (`expected_min_jobs: 20`,
  `zero_is_plausible: false`). State is seeded (`state/seen/`), no seed gap.
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
