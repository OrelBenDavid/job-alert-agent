# Task: scale job-alert-agent from 3 companies to ~152

Read this entire file before writing any code. Execute the phases in order. Stop at each
**CHECKPOINT** and wait for my confirmation — do not run all phases in one pass.

I am handing you this file plus three data files and doing nothing else myself. Every setup
step, file placement, and command run is your job, not mine. Do not end a phase by telling
me to run something; run it.

---

## Phase 0 — Ground yourself in the actual repo, then set up

### 0a. Reconciliation (do this first, before anything else)

This document was written without visibility into your current working copy. It may be
stale or wrong about the repo's real state — file layout, module names, how the
`career-site-profiler` skill currently works, what tests exist, which companies are already
wired in, or whether some of this work is already partly done.

**Where this document and the repo disagree, the repo wins.** You have the ground truth and
I do not. Do not contort the codebase to match a description here that no longer holds, and
do not re-do work that already exists.

Before Phase 1, inspect the repo and the skill as they actually are, then report to me:
- What this document gets wrong or assumes about the current state.
- Anything already implemented that a phase below would duplicate.
- Any instruction below you intend to deviate from, and why.

Treat what follows as intent and constraints — the goal, the risks I care about, and the
things I don't want broken. Adapt the mechanics to whatever the code actually looks like.

If a deviation is small and obviously correct, take it and note it. If it changes the
architecture or the scope, raise it at the nearest checkpoint before acting.

### 0b. Setup

Three files are provided alongside this prompt:
- `bot_shortlist.csv` — 152 companies. Columns: `tier, company, ats, id, api_endpoint,
  rnd_jobs, total_jobs, category, size, top_cities, hiring_evidence, id_source`
- `discover_ats.py` — a standalone probe/verify tool
- `israeli_companies_seed.csv` — ~7,940 Israeli companies (name, website, careers URL,
  size). Not used in the main path; it's the input for the optional expansion in Phase 6.

Create `_onboarding/` in the repo root, move all three there, and commit them. Choose a
different location if the repo's conventions call for one — just tell me where they went.

---

## Context

The project currently monitors 3 companies (Mobileye, Wiz, Wix). The 152 in the shortlist
all sit on four ATS platforms, every one with a public JSON API: Comeet (111),
Greenhouse (30), Lever (9), Ashby (2). None require a browser.

**Provenance of the input data, stated plainly:** the `id` column is well-sourced — either
company-declared in a public dataset (`mluggy/techmap`) or extracted from a live job URL —
but **not one row has been verified against a live API.** The `api_endpoint` column is
constructed from each platform's known URL pattern; it is a hypothesis, not a fact. The
Comeet endpoint format is the least certain element in the whole set, and Comeet is 73% of
the list. The `rnd_jobs` column counts open engineering roles (software / devops /
data-science / QA / security / hardware) from a job feed snapshot dated 2026-08-12 — it is
evidence the company was hiring engineers recently, not a guarantee they are today.

`tier` means: **A** = 3+ open R&D roles in the feed; **B** = 1–2; **C** = large known R&D
organisation with no rows in the feed, so hiring is genuinely unverified for those 27.

---

## Project conventions

- **All code comments in English.** This is a change — if you find Hebrew comments anywhere
  in the repo, convert them as you touch those files. Do not introduce new Hebrew comments.
- Identifiers and docstrings in English.
- Every fetch function returns `list[Job]` — the frozen dataclass with
  `id, title, location, url, company`. Never `list[str]`.
- `id` is the ATS's own job id when one exists, otherwise the job link through
  `canonicalize_url()`. Diffing and dedup are **always** by `id`, never by title or any
  display string.
- `url` is mandatory.
- `Job.display()` builds `"Title — Location"`, lives in the notification layer only, and is
  never stored, compared, or used as a key.
- Merging multiple sources for one company: merge into a dict keyed by `id`.
- Never assert a selector, endpoint, or pagination mechanism you have not verified with a
  real request. Anything unverified is written down as an explicit note, not stated as fact.

---

## Phase 1 — Verify all 152 before touching production code

Run `_onboarding/discover_ats.py verify _onboarding/bot_shortlist.csv` yourself.

Separately, and not trusting that script's assumptions, hit **one company per platform** by
hand and print the raw JSON so the real response shape is on the record. Derive the correct
endpoint from what actually comes back — do not accept my `api_endpoint` column.

For each of the four platforms, establish and write down:
1. The working endpoint URL template.
2. Whether the response paginates or returns everything at once. Test this against the
   company with the **most** jobs on that platform, not a small one — a 12-job board will
   never reveal a 50-per-page limit.
3. The exact JSON path to: stable job id, title, location string, absolute job URL.
4. Whether Greenhouse and Lever EU-hosted accounts need a different API host, and how to
   detect that at runtime. Mobileye is already known to be `api.eu.lever.co`.

Fix `discover_ats.py`'s adapters wherever reality differs, and re-run.

Output `_onboarding/verify_report.md`: a table of all 152 with live status and Israel job
count, plus a per-platform section covering the four points above.

**CHECKPOINT 1** — show me the report, the dead rows, and anything that surprised you.

If Comeet turns out to be broken or to need per-company investigation, stop and say so
plainly. That collapses the list from 152 to 41 and changes the plan, and I'd rather know
now than after the code is written.

---

## Phase 2 — Platform-level profiles

Do **not** write 152 profile.json files.

All 152 sit on four platforms, and for pure-API ATS platforms `fetch_type`,
`pagination.method` and `israel_filter.structure` are identical across every customer —
only the id differs. So the correct unit of a profile here is the platform, not the company.

Use the `career-site-profiler` skill to produce four platform-level profiles. Run it
properly for each: its Israel-filter step and its pagination-on-filtered-results step still
apply — the answer simply comes out the same for every company on that platform, which is
the point. Do Comeet first; it carries the most risk and the most companies.

Suggested layout (adjust to the repo's actual conventions):

```
profiles/_platforms/{greenhouse,lever,ashby,comeet}.json
profiles/companies/*.json
```

A company record is thin:

```json
{"company": "Wiz", "platform": "greenhouse", "id": "wizprivate"}
```

with optional per-company overrides, e.g. `"api_domain": "api.eu.lever.co"`.

Resolution rule: `platform` selects a `_platforms/*.json`; any key in the company record
overrides the platform profile. A company that doesn't fit its platform's shape may carry a
full standalone profile instead.

**This is config resolution, not a behaviour registry.** The dispatcher must still read
`fetch_type` / `pagination.method` / `israel_filter.structure` off the *resolved* profile
and act on those fields. Do not introduce a hardcoded map of platform name → function.

Migrate the existing companies onto this structure: Mobileye and Wiz become thin records.
**Wix keeps its full standalone playwright profile and must not be touched.** Every existing
test must still pass; if a test needs to change, show me why before changing it.

**CHECKPOINT 2** — show me the four platform profiles and the dispatcher diff.

---

## Phase 3 — Bulk import

Write and run a one-off, idempotent, committed script that turns the verified shortlist into
company records. It must skip every row that failed Phase 1. No hand-written company files.

Filenames: slugified company name. Handle collisions explicitly rather than silently
overwriting one company with another.

**CHECKPOINT 3** — file count and three sample records.

---

## Phase 4 — Make the runner survive 152 companies

These are scale problems that do not exist at 3 companies. Address each, and skip any that
the repo already handles:

1. **Concurrency.** 152 sequential HTTP calls is too slow for a scheduled Actions run. Use a
   thread pool (~10 workers). Respect per-platform rate limits; Ashby is the tightest.

2. **Failure isolation.** One company raising must not abort the run. Catch per company,
   collect failures, complete the rest, and report failures in a summary message kept
   distinct from job alerts.

3. **Sharded state.** A single state file holding job ids for 152 companies produces a huge
   diff on every commit. Split to `state/{company}.json` and write only files that actually
   changed, so the commit diff stays proportional to what happened.

4. **Silent-zero detection.** This is the failure mode that matters most here: a broken id
   returns an empty list, which is indistinguishable from "no open jobs" — the bot looks
   healthy while silently never alerting again. Persist a `last_count` per company. If a
   company that previously returned >0 returns exactly 0, treat it as a **fault**: do not
   clear its state, do not report every job as removed, and surface it in the failure
   summary.

5. **Notification batching.** With 152 companies one run can surface dozens of new jobs, and
   Telegram will rate-limit or drop them. Group by company and cap jobs per message. If a
   run produces an implausible number of new jobs (>50), send a warning instead of the
   flood — at that volume it is almost always a state bug, not a hiring spree.

6. **Runtime budget.** Private repo, so Actions minutes are metered. Measure and report
   actual wall-clock time for a full run. If it exceeds ~2 minutes, tell me and propose
   widening the cron from 3h rather than doing it unilaterally.

**CHECKPOINT 4** — full dry run against all verified companies, with timings.

---

## Phase 5 — Staged seed

Last, and carefully. Running against 152 fresh companies without seeding first emits
thousands of alerts at once and gets the bot rate-limited by Telegram.

`--seed` stays manual-only and never automatic. Extend it to take a batch —
`--seed --limit 25`, or a company-list file — so state fills in chunks of ~25.

Run the seed batches yourself, pausing between them, and report state size and any anomalies
after each. Only after seeding is complete and I have confirmed the state looks right does
the scheduled run go live. Do not enable the live schedule without that confirmation.

---

## Phase 6 — Optional expansion (only if I ask)

`israeli_companies_seed.csv` holds ~7,940 Israeli companies. `discover_ats.py discover`
brute-forces candidate slugs from each name against the platform APIs to find companies
nobody declared an ATS for. It is the only route to meaningfully broader coverage.

Don't run it now. Note it as available, and flag that it needs throttling and a review pass
on hits before anything is imported.

---

## Do not

- Touch the Wix profile or its playwright fetcher.
- Add a browser dependency for any of the 152. If one appears to need it, drop that company
  and tell me.
- Rewrite `Job`, the id-based diff, or `canonicalize_url()`.
- Assert anything about an endpoint you haven't actually called.
- Let a company's display name become a key anywhere.
- Enable the live schedule before Phase 5 is confirmed complete.
