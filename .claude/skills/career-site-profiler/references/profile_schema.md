# Company profile JSON schema (v3)

One file per company: `profiles/<slug>.json`. The profile **is** the company's registration in
the project — there is no separate `companies.json`. The dispatcher reads `fetch_type`,
`pagination.method` and `israel_filter.structure` from here and acts on them.

Every field must trace back to something actually verified during investigation. No placeholders
left unfilled, no guessed values. Anything you couldn't confirm goes in `notes` **as unconfirmed**.

**Ordering matters**: `israel_filter` is investigated and decided in Step 3a, *before* `pagination`
in Step 3b — because the filter is applied first and pagination is then investigated on the
already-filtered set. Keep that order in the file too.

## Required top-level fields

| Field | Type | Notes |
|---|---|---|
| `schema_version` | int | `3`. The loader accepts `2` and `3` and rejects anything else. A `2` profile is read as if `detail_fetch` were absent. |
| `slug` | string | lowercase, `[a-z0-9_]`, unique. Filename must match. |
| `name` | string | display name used in Telegram messages. |
| `enabled` | bool | `false` pauses the company without deleting the profile — this is what `/remove` sets, so history isn't lost. |
| `careers_url` | string | the resolved, real listing URL. |
| `resolved_from` | string | one plain sentence on how Step 0 landed here — especially if it wasn't the obvious guess. |
| `fetch_type` | `api` \| `html` \| `playwright` | |
| `israel_filter` | object | see below |
| `api` / `html` / `playwright` | object | include **only** the one matching `fetch_type` |
| `detail_fetch` | object | **optional** — how to reach a posting's full description text. Absent means the experience filter cannot evaluate this company; see below. |
| `health` | object | see below — not optional |
| `verified_on` | date | `YYYY-MM-DD` |
| `notes` | string | anything uncertain, stated plainly |

## Full example

```json
{
  "schema_version": 3,
  "slug": "mobileye",
  "name": "Mobileye",
  "enabled": true,
  "careers_url": "https://jobs.eu.lever.co/mobileye",
  "resolved_from": "company name 'Mobileye' — web_search returned both careers.mobileye.com/jobs (branded landing) and jobs.eu.lever.co/mobileye (Lever board) as separate results; picked the Lever one per Step 0.1",
  "fetch_type": "api",

  "israel_filter": {
    "method": "post_fetch",
    "structure": "flat_multi_location",
    "param": null,
    "matching_locations": null,
    "ui_actions": null,
    "checked_fields": ["categories.location"],
    "_note": "structure is recorded for documentation only — the site's UI picker is flat_multi_location, but this profile is fetch_type=api so the picker is never touched. The API returns every posting's location in one call and filtering is a post_fetch check on that field. Per SKILL.md Step 6, an api profile NEVER uses a Playwright location-picker template."
  },

  "api": {
    "platform": "lever",
    "endpoint": "https://api.eu.lever.co/v0/postings/mobileye?mode=json",
    "paginated": false,
    "location_query_param": null,
    "fields": {
      "id": "id",
      "title": "text",
      "location": "categories.location",
      "url": "hostedUrl"
    }
  },

  "health": {
    "expected_min_jobs": 12,
    "zero_is_plausible": false,
    "sentinel": null,
    "requires_browser": false
  },

  "verified_on": "2026-08-11",
  "notes": "Confirmed live: api.eu.lever.co returns a JSON array, all postings in one call, no pagination. The .eu subdomain matters — api.lever.co (non-EU) 404s for this slug. No detail_fetch block: Lever's mode=json response is widely reported to carry the posting body, but that was not confirmed against this slug, and an unverified block must not be written."
}
```

Note what the example does **not** contain: there is no `detail_fetch`. That is deliberate and is the
correct output when Step 3c couldn't verify description access. Omitting the block degrades one
feature; writing a guessed selector into it silently mis-filters real jobs out of the alerts.

## `israel_filter` block

| Field | Notes |
|---|---|
| `method` | `url_param` \| `ui_interaction` \| `post_fetch`. Always `post_fetch` for `fetch_type: api`. |
| `structure` | `flat` \| `hierarchical` \| `flat_multi_location` \| `none`. `flat` = selecting Israel filters directly. `hierarchical` = the country entry is a group that must be expanded and all its children selected. `flat_multi_location` = only individual "City, Country" entries, single-select, so each Israeli city needs its own fetch and the results get merged. `none` = the site has no location filter. |
| `param` | **mandatory** if `method = url_param`, and also if `structure = flat_multi_location` (the per-city query parameter). For `hierarchical`, may be a repeated or comma-joined param covering every city. |
| `matching_locations` | only for `flat_multi_location` — the option strings observed in the picker whose country is Israel. **Discovered dynamically at runtime, not read from here** — this field is documentation of what was seen on `verified_on`, not the operating list. |
| `ui_actions` | only if `method = ui_interaction`. Prose: the ordered steps as observed, for a human reader. **Documentation only — no code reads this field.** |
| `ui_actions_structured` | **mandatory whenever `method = ui_interaction`** — this is the one the fetcher replays. A list of `{"action": ..., ...}` steps; see below. |
| `checked_fields` | which fields the relevance check actually reads. For Greenhouse this must include `offices` as well as `location.name`, because `"Multiple Locations"` is common and defeats a name-only check. |
| `reapply_after_navigation` | bool, documentation only. The fetcher **always** re-applies the filter after every navigation — the safe direction — so this field records what you observed, it does not switch anything off. |

### `ui_actions_structured` — the replayed step list

`ui_actions` is prose for a human. `ui_actions_structured` is what
`fetchers/browser.py` actually executes, and a `ui_interaction` profile without it
**fails to load**. That is deliberate: with an empty step list nothing raises, no
location filter is applied, and the fetcher paginates the company's entire
unfiltered listing until `max_pages` cuts it off — so Israeli postings past the cap
are silently never seen, while the job count stays healthy enough that the health
gate notices nothing.

```json
"ui_actions_structured": [
  {"action": "click", "selector": "button[data-testid='location-filter']"},
  {"action": "fill",  "selector": "input[name='location-search']", "value": "Israel"},
  {"action": "press", "value": "Enter"},
  {"action": "wait",  "value": 1500}
]
```

| `action` | Needs | Does |
|---|---|---|
| `click` | `selector` | `page.click(selector)` |
| `fill` | `selector`, `value` | `page.fill(selector, value)` |
| `press` | `value` | `page.keyboard.press(value)` |
| `wait` | `value` (ms) | `page.wait_for_timeout(value)` |

Any other verb is rejected at load time.

Note: the relevance keyword list (Israeli cities, Hebrew names, the qualified-remote rules) lives in
**one place in code** — `RELEVANCE_HELPER` in `assets/function_templates.py`. It is deliberately not
duplicated per profile, so a fix reaches every company at once.

## `api` block

```json
{
  "platform": "lever | greenhouse | comeet | smartrecruiters",
  "endpoint": "full URL as actually called",
  "paginated": false,
  "pagination_params": "only if paginated — e.g. {\"offset\": \"offset\", \"limit\": \"limit\", \"total\": \"totalFound\"}",
  "location_query_param": "if the API accepts a server-side location filter, note it here",
  "fields": {"id": "...", "title": "...", "location": "...", "url": "..."}
}
```

`fields.id` and `fields.url` are **mandatory** — the whole diff mechanism runs on the id.

**`platform` is restricted to the four platforms that have a handler in
`fetchers/api.py`, and the profile loader rejects anything else.** Step 1a's table
lists four more (Ashby, Workable, Recruitee, Workday) precisely because they are
the ones nobody has verified; if an investigation confirms one live, that is a
dispatcher change — a new handler plus a new entry in
`profiles.IMPLEMENTED_API_PLATFORMS` — not something a profile can declare on its
own. Writing one anyway used to load cleanly and then fail once per run, per
company, as an ordinary fetch error that stays quiet until it has failed twice.

## `html` block

```json
{
  "source": "css | json-ld",
  "job_selector": "CSS selector for each job card",
  "title_selector": "relative to the card",
  "location_selector": "relative to the card",
  "link_selector": "relative to the card — mandatory, this becomes the job id",
  "link_base": "absolute base to resolve relative hrefs against, if they're relative"
}
```

## `playwright` block

```json
{
  "job_selector": "...",
  "title_selector": "...",
  "location_selector": "...",
  "link_selector": "mandatory",
  "link_base": "...",
  "scroll_container_selector": "only if pagination.method = scroll AND the list scrolls inside an inner element rather than the window",
  "location_filter_selector": "MANDATORY when israel_filter.structure = flat_multi_location - the control that opens the location picker",
  "location_option_selector": "MANDATORY when israel_filter.structure = flat_multi_location - the individual options inside that picker, read to discover the Israeli cities at runtime",
  "pagination": {
    "method": "none | url_param | click | scroll",
    "param_name": "only if url_param",
    "start_value": 1,
    "max_pages": 50,
    "max_seconds": "optional wall-clock ceiling for this company's whole paginated walk (default 240). Exceeding it raises a fetch error rather than returning a partial walk, which would be indistinguishable from a shorter listing.",
    "first_page_timeout_ms": "optional, default 30000 - how long to wait for the FIRST page's cards before treating it as 'the listing never rendered'. Raise it for a heavy, slow careers page.",
    "next_page_timeout_ms": "optional, default 10000 - how long to wait on any LATER page before concluding there is no page N. This one is an ordinary end-of-pagination signal, not a failure, which is why it is much shorter.",
    "out_of_range_behavior": "what ?page=999 actually did — 'empty list' or 'clamps to page 1' or 'not tested'. If it clamps, a naive loop never terminates.",
    "next_button_selector": "only if click",
    "disabled_marker": "how a disabled Next is expressed — e.g. 'aria-disabled=true' or 'class contains disabled'. Playwright's is_disabled() does NOT catch either of these.",
    "stop_condition": "plain-language description of how you detected the end, and whether you actually reached it",
    "_note": "investigated ON the already-filtered view, not the full site — counts here may be much smaller than the site's total"
  }
}
```

## `detail_fetch` block — optional, new in v3

Describes how to obtain a **single posting's full description text**. It exists for one consumer:
the experience filter, which parses that text for a minimum-years-of-experience requirement and
suppresses postings above the user's threshold.

This block is about the **notification layer**, not discovery. It is never used to find jobs, never
affects `id`, and never affects the diff.

```json
"detail_fetch": {
  "method": "inline",
  "inline_field": "lists",
  "inline_section_heading": "text",
  "inline_section_content": "content",
  "url_source": null,
  "url_template": null,
  "content_selector": null,
  "content_is_html": true,
  "requirements_section_selector": null,
  "wait_for": null,
  "verified_on_job_url": "https://jobs.eu.lever.co/mobileye/<real-id-seen>",
  "_note": "free — the listing call already returns the body, no extra request per job. `lists`, NOT descriptionPlain: see the Lever note in SKILL.md Step 3c.1, verified across 25 postings on this slug."
}
```

| Field | Notes |
|---|---|
| `method` | `inline` \| `html` \| `playwright` \| `none`. `inline` = the description is already in the listing response, so the filter costs **zero** extra requests. `html` = one plain fetch per *new* posting. `playwright` = the description only exists after JS renders. `none` = investigated and no reasonable access found — recorded explicitly so it reads as a finding, not an omission. |
| `inline_field` | `inline` only. Dotted path into the posting object, same notation as `api.fields` (e.g. `content`, `lists`). The value it resolves to may be a string **or an array of sections** — see below. |
| `inline_section_heading` | Only when `inline_field` resolves to an array. Key holding each section's heading. Default `"text"` (Lever's name for it). |
| `inline_section_content` | Only when `inline_field` resolves to an array. Key holding each section's body. Default `"content"` (Lever's name for it). |
| `url_source` | `html`/`playwright` only. `job_url` = reuse the `url` already on the `Job` (the normal case). `template` = the description lives at a different address. |
| `url_template` | only when `url_source: template`. May reference `{id}` and `{url}`. |
| `content_selector` | `html`/`playwright` only. CSS selector wrapping the description body. |
| `content_is_html` | whether the retrieved content is HTML that must be reduced to text before parsing. For Greenhouse `content` this is `true` **and** the value is HTML-escaped, so it needs unescaping before tag-stripping. |
| `requirements_section_selector` | optional. Isolates the requirements/qualifications block. When present the parser reads it first and falls back to the whole text if it isn't found. This is the single highest-value field for accuracy — it is what keeps a "5 years preferred" line in a *nice-to-have* section from disqualifying an entry-level role. |
| `wait_for` | `playwright` only. Selector that signals the description finished rendering. |
| `verified_on_job_url` | **mandatory whenever `method` is not `none`.** The actual posting URL against which the field or selector was confirmed. A block without it is unverified and must not be written. |

### When the inline field is structured

Some listings return the description already split into sections rather than as one
string. Lever is the case that forced this: `lists` comes back as

```json
[{"text": "What will your job look like:", "content": "<li>...</li><li>...</li>"},
 {"text": "All you need is:",             "content": "<li>...</li><li>...</li>"},
 {"text": "Nice to have:",                "content": "<li>...</li>"}]
```

Point `inline_field` at the array and the consumer flattens it to HTML, emitting each
section's heading as a real heading and wrapping its bullets in a list. Use
`inline_section_heading` / `inline_section_content` only if a platform names those keys
differently from Lever's `text` / `content`.

Keeping the headings is the point, not a nicety. "All you need is:" and "Nice to have:"
are what let the parser tell a requirement from an aspiration, and they're recruiter-written
so they can't be found by selector. A structured field hands you that boundary for free —
flattening it into one blob throws away the most valuable thing it has.

### Cost ordering (mirrors the project's cheap-to-expensive rule)

`inline` > `html` > `playwright`. Check for an inline field before assuming a per-job request is
needed. A `fetch_type: api` profile whose API does *not* return the body legitimately takes
`method: html` — the cheap-to-expensive rule governs how the **listing** is fetched, and a per-job
HTML fetch is still the cheapest way to reach a description that the API withholds.

### What absence means

A profile with no `detail_fetch`, or with `method: none`, is not broken. The experience filter is
**fail-open**: a posting whose required experience cannot be determined is delivered, tagged as
undetermined. So an unverified block costs one degraded feature for one company, while a *guessed*
block costs relevant jobs that are silently never delivered. When in doubt, omit.

For the same reason, a runtime failure here (404 on the posting page, selector no longer matching,
timeout) must not mark the company unhealthy, must not block state from being written, and must not
abort the run. It resolves to "undetermined" and the posting is sent.

## `health` block — required for every profile

```json
{
  "expected_min_jobs": 12,
  "zero_is_plausible": false,
  "sentinel": "CSS selector or text that must exist on a healthy page even with zero results",
  "requires_browser": true
}
```

`zero_is_plausible: true` asserts that the company genuinely has no Israeli openings, backed by evidence written into `notes`. It is not a way to dismiss a zero count that actually indicates the wrong board or a broken selector — see Step 7.0 in `SKILL.md`. Setting it wrongly disables the one mechanism that would have caught the mistake later.

Silent breakage is caught three ways (all skipped when `zero_is_plausible` is `true`). Each leaves
saved state untouched and raises a maintenance alert after the repeated-failure threshold:

- **the count fell to 0** after any previously healthy run — a dead `job_selector`;
- **the count fell below `expected_min_jobs`**, from a run that was itself above it — good at
  *slow decay*, a drift downwards over weeks that no run-to-run comparison can see;
- **the count fell below 40% of the last healthy run** (once that run had at least 10 jobs) — good
  at *sudden breakage*, and it needs no number in the profile at all.

The last two cover opposite failure shapes, which is why both exist. The ratio is the one that
scales — it measures a company against its own history and never goes stale — so
**`expected_min_jobs` should be set lazily: about half the count you observed, rounded down, no
deliberation.** A tighter number feels more rigorous and is actually worse: it fires on ordinary
churn, and each false fire freezes new-job detection for that company for three runs. Avoid `0`
unless the company genuinely has nothing, since that disables the check.

The two partial checks **give up after three consecutive runs**, accept the lower count as the new
normal, and resume detecting — because while the gate holds, no new jobs are reported for that
company at all, so a wrong floor must not be able to silence it indefinitely. A total zero has no
such cost and stays frozen until a human looks.

`sentinel` is **documentation only** — no code reads it today. Record it anyway: it is the note that
tells whoever rebuilds this profile how to tell a rendered-but-empty page from one that never
rendered.

`requires_browser: true` carries a known unverifiable risk: GitHub Actions IP ranges are often
blocked by Cloudflare/Akamai/PerimeterX on careers sites, so a page that loaded fine during
investigation can still 403 from a runner. Say so in `notes`; the first real run is the test.

Omit `api` if `fetch_type` isn't `api`, `html` if not `html`, `playwright` if not `playwright`.
