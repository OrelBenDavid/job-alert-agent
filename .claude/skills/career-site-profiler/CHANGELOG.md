# Changelog

This skill lives in exactly one place for this project:
`.claude/skills/career-site-profiler/`, committed to git. There is no
separate "versions" folder — git IS the version history for this path.
Every entry below corresponds to a real commit; run

```
git log -- .claude/skills/career-site-profiler
```

from the repo root to see them, or `git show <commit>:.claude/skills/career-site-profiler/SKILL.md`
to read any past version in full. The working-tree copy (what's on disk right
now) is always the current version; nothing below needs to be manually kept
in sync with it.

## 2026-08-13 — Sync fixes, found by comparing the skill against the code it feeds

Committed as `46a0be6`. All of the following were mismatches between what
this skill instructed and what `src/` in this repo actually reads — each one
would have produced a profile that loads, runs, and silently returns the
wrong jobs.

- **`israel_filter.ui_actions_structured` is now required for `ui_interaction`
  profiles**, not just the prose `ui_actions` field. Without it, the fetcher
  applies no location filter at all — nothing raises, but pagination then
  walks the entire unfiltered listing and `max_pages` truncates it, so
  Israeli postings past the cap are silently never seen. `profiles.py`
  rejects a profile missing this at load time; the skill previously only
  documented the prose field.
- **`api.platform` restricted to the four platforms with an implemented
  handler** (`lever`, `greenhouse`, `comeet`, `smartrecruiters`). The old
  guidance offered the full eight-platform table including four unverified
  ones with no code to run them.
- **`health.expected_min_jobs`: "set it lazily."** Documented as roughly half
  the observed count, deliberately not deliberated over — a tight number
  feels more rigorous but actually fires on ordinary churn, and each false
  fire now freezes new-job detection at that company for three runs (see the
  next point).
- **The health gate is now three checks, not one**, and the skill documents
  all three: total zero (dead selector), below `expected_min_jobs` from a
  run that cleared it (slow decay), and below 40% of the last healthy run
  (sudden breakage, needs no per-profile number). Partial collapses accept
  the new count after three consecutive runs rather than freezing forever.
- **The profile is the deliverable for this project, not a generated Python
  function.** `src/fetchers/` already implements every template generically;
  Steps 6 and 8 previously told the skill to hand over a per-company
  function that this project has nowhere to put.
- **Foreign-city markers added to the relevance helper's exclusion list.**
  Remote roles anchored to a specific city ("Remote - New York",
  "Hybrid - Boston") were previously kept, since the list only covered
  countries and a handful of capitals.
- **Recovered content from a stale packaged `.skill` bundle that had drifted
  ahead of the working folder** this was originally edited from: the warning
  to check an inline description field actually contains *requirements* and
  not merely *text*, the Lever finding that `description`/`descriptionBody`
  yield a number on 0 of 25 postings while `lists` yields 18, and the
  "when the inline field is structured" section in the schema reference.

## Earlier — v3 (pre-dates this repo's copy)

Baseline v3: added `detail_fetch` (description access for the experience
filter), the Step 7.0 completeness gate, and `schema_version: 3` (v2 profiles
still read as having no `detail_fetch`). See `SKILL.md`'s own "What changed
in v3" section — that history predates this skill being vendored here and
isn't duplicated in this changelog.
