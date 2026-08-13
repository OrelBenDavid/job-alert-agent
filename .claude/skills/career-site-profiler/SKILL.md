---
name: career-site-profiler
description: Builds a reusable "profile" for a company's careers/jobs page, starting from just a company name - finds the real careers URL, then figures out how to programmatically pull its open positions - which access method works (official ATS API like Lever/Greenhouse/Comeet/SmartRecruiters/Ashby/Workable/Recruitee vs. HTML scraping vs. browser automation), how to filter to Israel-relevant roles before fetching anything (not after), and how pagination works (URL page parameter, click-based "Next" button, or infinite scroll) on the already-filtered results. Ends by generating a ready-to-use Python function matching that exact profile. Use whenever the user gives a company name to add to a job-monitoring/job-alert bot, asks how to pull jobs from a company, or wants a careers page investigated the way it was done for Wix or Mobileye. Prefer this skill over ad-hoc investigation whenever the task is figuring out how to scrape jobs from a given company.
---

# Career Site Profiler (v3)

## What changed in v3

- **New Step 3c: description access.** Every profile now also records *how to reach one posting's full description text*, in the optional `detail_fetch` block (`references/profile_schema.md`). It feeds the experience filter, which parses minimum-years-of-experience from that text. Step 3c runs on **every** path, including the API fast-path out of Step 1a/1b that never opens a browser.
- **`schema_version` is now `3`.** A v2 profile is still valid and is read as having no `detail_fetch`.
- **New Step 7.0: completeness gate.** Everything else in this skill verifies that a source *works*; nothing verified that it's the *right* source. A zero or implausibly small post-filter count now sends you back to Step 0/1b instead of being recorded as a legitimate result. Step 1a carries a matching warning that a working ATS identifier is not necessarily the company's main one.
- **`inline_field` may resolve to an array of sections, not just a string** (Lever's `lists`). Correcting an earlier version of this guidance: on Lever the plain description fields carry only the intro paragraph, and pointing at them produces a block that validates and determines nothing. Step 3c.1 has the measurements.
- Omitting `detail_fetch` is an acceptable, sometimes correct outcome — the filter is fail-open, so an absent block degrades one feature, while a guessed one silently withholds real jobs.

Investigates a single company's careers page and produces:
1. A **profile** (JSON) documenting exactly how to fetch its jobs — per `references/profile_schema.md`. In the job-alert project this is the whole deliverable: `src/fetchers/` already implements every template generically and dispatches on fields read from the profile.
2. Only when the user is working *outside* that project: a **Python function**, generated from a template in `assets/function_templates.py`. See Step 6.

The whole point: don't guess. Every claim in the profile (pagination style, selector, filter behavior) must be verified against the real site before it goes in the profile. Anything unverified is written down **as unverified**, never presented as fact.

## What changed in v2 (read this if you used v1)

- **Return type is `list[Job]`, not `list[str]`.** Every fetch function must capture a stable `id` and a `url` per posting. Deduplication and new-job detection run on `id`. The `"Title — Location"` string is built later, in the notification layer only. A function that cannot produce a per-job link is incomplete.
- **Step 1 now greps the page HTML for embedded ATS signatures**, not just the URL pattern. Embedded Greenhouse/Comeet/Lever widgets on a branded domain were being missed and sent down the Playwright path for no reason.
- **Expanded ATS table** (Ashby, Workable, Recruitee, Workday added).
- **"Israel filter" is now "relevance filter"** — Israeli locations *plus* qualified remote (see Step 5).
- **New Step 8: health & failure semantics.** A profile is not finished until it declares how the caller can tell "0 jobs because nothing is open" apart from "0 jobs because the selector broke".
- The templates in `assets/function_templates.py` were rewritten to fix real bugs (infinite pagination loops, `is_disabled()` not catching `aria-disabled`, premature scroll stop, missing URL-encoding, missing per-city pagination, and a UI filter being destroyed by `page.goto` in the URL-pagination loop). Use v2 templates only.

## When to reach for browser tools vs. plain requests

Try things in this order — stop as soon as one works, because each step is more expensive than the last:

1. **Known ATS by URL pattern** (Step 1a) — no browser needed at all.
2. **Known ATS by HTML signature** (Step 1b) — one `web_fetch`, still no browser.
3. **Plain `web_fetch` of the careers URL** (Step 2) — check if job titles already appear in the raw HTML (many sites server-render for SEO even if they *look* JS-heavy).
4. **claude-in-chrome browser tools** (Step 3) — only when the above don't yield the job list. This is the expensive path, and it's also the one most likely to break later or get blocked at runtime (see Step 8).

## Step 0 — Resolve the company name to an actual careers URL

The input to this skill is a **company name**, not a URL — finding the real careers page is part of the job, every time.

1. `web_search` for `<company name> careers jobs` (or `<company name> open positions`). **Scan every result returned, not just the first one** — before picking a "main" domain, check whether any result's URL already matches a known ATS pattern from Step 1a's table. Search engines frequently index the ATS-hosted job board as its own separate result, distinct from the company's branded landing page — if one shows up, **that's the URL to use, directly**, even if the company's own branded domain (`careers.<company>.com`) also appears and looks equally "official." This is exactly how Mobileye was resolved: the branded page `careers.mobileye.com/jobs` genuinely exists and presents itself as an open-positions page, but the functional, scrapable board is a *different* URL, `jobs.eu.lever.co/mobileye`, which turned up as its own separate search result.
2. **Reject third-party aggregators** even if they rank first — LinkedIn Jobs, Glassdoor, Indeed, ZipRecruiter, Wellfound and similar are not the company's own page and must not become the `careers_url`. They're useful only as a clue (Glassdoor sometimes reveals the real ATS in a job's "Apply" link) — never as the source.
3. If the first result is ambiguous (multiple similarly-named companies, a regional subsidiary), do a second, more specific search (add the industry, city, or "official site") before picking — don't guess between candidates.
4. If step 1 didn't surface a known-ATS URL, fall back to the company's **own domain** (`careers.<company>.com`, `<company>.com/careers`, `jobs.<company>.com`). `web_fetch` it once to sanity-check it's actually a careers/jobs listing and not an "About us" or a dead link. If it redirects, follow it and use the final URL.
5. **Even a real, working branded page can still be a landing page that links out to the actual board with no HTTP redirect** — check explicitly. Look through the fetched content for a "See open positions" / "View all jobs" call-to-action that's a plain `<a href>` to a different domain, `web_fetch` that too, and re-run Step 1a against *that* URL.
6. Only once you have a confirmed, real careers/jobs *listing* URL, proceed to Step 1.

If the company has no public careers page at all (rare, but happens for very small or non-hiring companies), stop here and say so — don't force a profile onto a site that isn't a jobs listing.

## Step 1a — Check for a known ATS platform by URL pattern

Many companies use a third-party recruiting platform with a free public API. If the careers URL matches one of these, skip everything else — use the API.

| Platform | URL pattern | API | Status |
|---|---|---|---|
| Lever | `jobs.lever.co/<slug>`, `jobs.eu.lever.co/<slug>` | `GET https://api.lever.co/v0/postings/<slug>?mode=json` (or `api.eu.lever.co`) | verified |
| Greenhouse | `job-boards.greenhouse.io/<token>`, `boards.greenhouse.io/<token>` | `GET https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true` | verified |
| Comeet | `comeet.com` in URL or page source | `GET https://www.comeet.com/careers-api/2.0/company/<uid>/positions` (often needs `?token=<token>`) | **partially verified — see note** |
| SmartRecruiters | `jobs.smartrecruiters.com/<company>` | `GET https://api.smartrecruiters.com/v1/companies/<company>/postings` | verified (paginated) |
| Ashby | `jobs.ashbyhq.com/<slug>` | `GET https://api.ashbyhq.com/posting-api/job-board/<slug>?includeCompensation=true` | **UNVERIFIED — must be confirmed with a live call before it goes in a profile** |
| Workable | `apply.workable.com/<slug>` | `GET https://apply.workable.com/api/v1/widget/accounts/<slug>?details=true` | **UNVERIFIED** |
| Recruitee | `<slug>.recruitee.com` | `GET https://<slug>.recruitee.com/api/offers/` | **UNVERIFIED** |
| Workday | `<co>.wd{1,2,3,5}.myworkdayjobs.com/<site>` | `POST https://<co>.wd3.myworkdayjobs.com/wday/cxs/<co>/<site>/jobs` with a JSON body (`limit`, `offset`, `appliedFacets`) | **UNVERIFIED — heavier than the others, but still far cheaper than Playwright** |

The first four return clean JSON, no auth, and (except SmartRecruiters) everything in one call. See `references/api_platforms.md` for exact field names per platform.

**Rule for the four marked UNVERIFIED: never write one into a profile from this table alone.** Make the actual call, look at the actual response, and record what you saw. If the call fails, say so and fall through to Step 1b/2 — do not ship a guessed endpoint.

**A working identifier is not necessarily the right identifier.** Large companies routinely run more than one board on the same platform — private/confidential boards, post-acquisition boards, regional boards. A token pulled from a search result can return 200, valid JSON, and real postings while representing a small fraction of the company's openings. Whenever the identifier came from a search result rather than from the company's own careers page, still run Step 1b against the branded domain and compare. Step 7.0 is the backstop for this, but catching it here is cheaper.

If a platform matches and the call succeeds, skip the browser work in Step 3 — but **still do Step 3c** (description access, needed for the experience filter), then **Step 5** (relevance filter) and **Step 6** (generate function). Step 3c applies to every profile regardless of how the listing is fetched.

## Step 1b — Check for a known ATS embedded in the page HTML

**This is the most common miss, and it is new in v2.** A company can use Greenhouse/Comeet/Lever/Ashby while its careers URL looks completely branded (`careers.company.com`), because the board is embedded as an iframe or a JS widget. Step 1a's URL check will say "no ATS" and send you to Playwright for no reason.

So: `web_fetch` the careers URL and search the returned HTML for these signatures before concluding anything.

| Signature in HTML | Means | How to extract the identifier |
|---|---|---|
| `grnhse_app`, `boards.greenhouse.io/embed/job_board?for=<token>` | Greenhouse embed | the `for=` value is the board token |
| `comeet.com/careers-api`, `comeet-position`, `data-comeet-...` | Comeet widget | the company UID (and often a `token`) appear in the widget's script/iframe URL |
| `jobs.lever.co`, `lever-jobs`, `api.lever.co` | Lever embed | the slug is the path segment after the domain |
| `ashbyhq.com/<slug>`, `_ashby_embed` | Ashby embed | slug from the URL |
| `myworkdayjobs.com` | Workday iframe | company + site from the path |
| `smartrecruiters.com` | SmartRecruiters widget | company slug from the path |
| `<script type="application/ld+json">` with `"@type": "JobPosting"` | Google-Jobs SEO markup | not an ATS, but see Step 2 — it's a free structured feed |

If a signature hits, extract the identifier, make the real API call to confirm it returns postings, and treat the profile as `fetch_type: api` — then continue at **Step 3c**, not Step 6. Only if nothing hits do you continue to Step 2.

## Step 2 — Try a plain fetch before opening a browser

```
web_fetch(careers_url)
```

Look for actual job titles in the returned content. Two ways this can succeed:

- **Server-rendered HTML** — titles are present in markup. Note the CSS selectors for title, location, **and the link `<a href>`** (v2: the link is mandatory, it becomes the job's `id`). `fetch_type: html`.
- **JSON-LD** — a `<script type="application/ld+json">` block with `@type: JobPosting` entries. This is often cleaner than scraping the DOM and it usually carries `url`, `title`, `jobLocation` directly. Prefer it over CSS selectors when both exist, and record `html.source: "json-ld"` in the profile.

If the content is an empty skeleton ("Loading…", empty containers, framework boilerplate) — move to Step 3.

## Step 3 — Browser investigation (only if Steps 1–2 didn't resolve it)

Use the `claude-in-chrome` tools. Load them with `tool_search` if not already available (navigate, computer, find, read_network_requests, read_page).

**Golden rule: find and apply the location filter *first*, before looking at pagination at all.** Investigating pagination on the unfiltered list is wasted work — filtering first can collapse 5 pages into 1, or change the pagination mechanism entirely.

### Step 3a — Find and apply the location filter (before anything else)

1. `navigate` to the careers URL, screenshot to see the real layout.
2. `find` for a location/country/region filter control (dropdown, search-with-suggestions, checkbox list, chip selector — whatever form it takes).
3. Interact with it to select **Israel**. Screenshot immediately after and check what actually happened — don't assume the click filtered anything:
   - **Flat filter (the simple, common case)**: clicking "Israel" immediately re-filters the list. Go to sub-step 4.
   - **Hierarchical/grouped filter**: clicking "Israel" (or a "+"/expand icon next to it) does **not** filter — it expands into individual cities/offices, each with its own checkbox, and the country entry is just a group header. If so: expand the group, select **every city currently listed under it** (the rule is "select all children of the Israel group", not a hardcoded city list — offices change over time), then check the URL per sub-step 4. If it's UI-only state, the full expand-and-select-all sequence becomes the profile's `ui_actions`.
   - **Flat single-select list of combined "City, Country" entries, no aggregate country option** — verified on Lever's own UI: the dropdown has no "Israel" entry, only options like "Haifa, Israel", "Jerusalem, Israel", "Tel-Aviv, Israel" mixed with other countries — and it's **single-select** (selecting a second city un-selects the first, confirmed). If so:
     a. Open the dropdown, read every option's text, keep the subset whose country component is Israel (discover dynamically — don't hardcode).
     b. Select the first matching city and check the URL — on Lever this produces `?location=Jerusalem%2C%20Israel`.
     c. Because only one city can be selected at a time, there is no combined "all of Israel" state. The fetch function must **loop over every matching city** (each with its own URL and its own possible pagination) and **merge + de-duplicate by job id/link**. Flag this as `israel_filter.structure = flat_multi_location`.
     d. **Before building any of that, re-check Step 1a/1b.** This exact pattern (Lever) already has a public API returning every posting's location in one call. The multi-location loop is only for sites with `fetch_type: playwright` — genuinely no API — that happen to use this picker style.
   - **No location filter at all**: fall back to post-fetch filtering (Step 5).
4. **Check whether the filter is expressed in the URL.**
   - **URL-based (prefer — cheapest, no UI automation at fetch time)**: if selecting Israel changed the URL (`?location=Israel`, `?country=IL`), verify by navigating **directly** to that URL in a fresh navigation with no prior clicks. If it lands pre-filtered, the param alone drives it. Bake it into `base_url`; the fetch function never touches the filter UI.
   - **UI-only (no URL change)**: state lives in JS. The fetcher must replay the exact interaction inside Playwright before looking for job cards. Record the precise selectors and ordered steps — this gets replayed identically on every run, so it needs the same rigor as pagination.
     > **Record them in `israel_filter.ui_actions_structured`, the machine-readable step list** (`{"action": "click"|"fill"|"press"|"wait", "selector": …, "value": …}` — see `references/profile_schema.md`). `ui_actions` is prose for a human reader and **no code reads it**. A `ui_interaction` profile without `ui_actions_structured` is rejected at load, and that check exists because the alternative is silent: with no steps to replay, no filter is applied, the fetcher paginates the company's *entire* unfiltered listing, `max_pages` truncates it, and every Israeli posting past the cap is never seen — while the job count stays healthy enough that nothing downstream notices.
     > **v2 warning:** if the filter is UI-only **and** pagination turns out to be `url_param`, note it explicitly in the profile. Each `page.goto` in the pagination loop wipes the filter state, so the generated function must re-apply the filter after every navigation. The v2 `TEMPLATE_PLAYWRIGHT_URL_PAGES` handles this via an `apply_relevance_filter(page)` helper called after each goto — but only if you record the actions. (v1's template got this wrong and silently returned unfiltered pages 2+.)
   - **No filter exists**: Step 5 fallback.

### Step 3b — Now investigate pagination, on the filtered view

1. `find` for the job listing container and, separately, for any pagination control ("pagination", "next button", "load more", "showing X of Y"). With filtering applied first there may be far fewer results — possibly a single page.
2. **Determine the mechanism**:
   - **URL-based paging (cheapest, prefer)**: click "Next" once, check whether the tab URL changed (e.g. `?page=2`, on top of any filter param). If yes, confirm by navigating **directly** to that combined URL with no clicks — if the content differs from page 1, it's confirmed. Note the param name, the start value (0- or 1-indexed — check, don't assume), and how the last page is detected.
     > **v2 required check:** navigate to an obviously-out-of-range page (e.g. `?page=999`) and record what happens. Many sites **clamp** and serve page 1 again instead of an empty list. If they clamp, a naive `while True` loop never terminates and the GitHub Actions job runs until the 6-hour timeout. Write the observed behavior into `pagination.stop_condition` verbatim. The v2 template also carries a hard `MAX_PAGES` cap and a per-page content fingerprint as a second line of defence, but the profile should still say what the site actually does.
   - **Click-based paging (no URL change)**: the "Next" button exists but the URL stays fixed. Note the button's selector **and how a disabled state is expressed** — this matters: most careers sites use `<a class="disabled">` or `aria-disabled="true"`, and Playwright's `is_disabled()` returns `False` for those (it checks the form `disabled` property). Record the actual disabled marker you observed. The v2 template checks `aria-disabled`, class names, and whether the page content changed at all after the click.
   - **Infinite scroll (no control at all)**: content grows as you scroll. Use `read_network_requests` (clear, scroll, read) to check whether scrolling triggers a distinct paginated API call you could hit directly instead of automating a browser. If not, note that the function needs a scroll loop. Also record **what scrolls** — the window, or an inner container? `page.mouse.wheel` does nothing if the list lives in an overflowing `div`; in that case record the container's selector so the template can scroll it via JS instead.
   - **No pagination**: everything (already filtered) is on one page. Simplest case — increasingly likely once filtering happens first.
3. Use `read_network_requests` (with `clear: true` right before the action) to check for an underlying JSON API in **all** cases — even for click/scroll pagination there is sometimes a clean endpoint underneath that's easier to call than automating a browser. Bonus: that endpoint may accept a location param, letting you skip the UI filter too. If found, prefer it.
4. Note the CSS selector for each job's **title, location, and link href**. The link is not optional in v2 — without it there is no stable id, and the bot will re-alert on cosmetic text changes.

**Do not assume — verify.** Every filter claim and every pagination claim must be tested before it goes in the profile. See `references/investigation_playbook.md` for a full worked trace.

### Step 3c — How to reach a posting's description text (new in v3)

**This step runs for every profile, on every path — including the ones that jumped straight here from Step 1a or 1b without ever opening a browser.** It fills the optional `detail_fetch` block, which exists so the experience filter can read a posting's requirements and suppress roles demanding more than the user's threshold.

Work cheapest-first, and stop at the first one that works:

1. **Inline — is the body already in the listing response?** Look at a single posting object from the call you already made. If it carries the description, you are done: `method: inline`, record the field path, and the filter costs **zero** extra requests forever.

   **Check that the field contains *requirements*, not merely that it contains *text*.** This is the trap in this whole step. A populated, plausible-looking description field can hold nothing but the marketing intro, and a block built on it is indistinguishable from a working one: it validates, it has a real `verified_on_job_url`, it returns content on every posting — and it determines nothing, so every role is delivered tagged "undetermined" and the user quietly gets no filtering at all. Read the actual value and look for a phrase like "3+ years of experience". If several candidate fields exist, compare them across a handful of postings rather than taking the first non-empty one; if none carries requirements, that platform's inline path has failed and you move to step 2, even though a description field exists.

   Two known candidates worth checking explicitly, neither of which may be assumed without a live call:
   - Greenhouse: `?content=true` is already requested for the `offices` array (Step 5) — check whether the same response carries a `content` field. If it does, note that its value is HTML **and** HTML-escaped.
   - Lever: `?mode=json` is already the endpoint in use. It returns several description-shaped fields, and **the obvious ones are the wrong ones.** `description`, `descriptionPlain`, `descriptionBody` and `descriptionBodyPlain` hold only the posting's intro paragraph; the requirement bullets live exclusively in **`lists`**, a structured array of `{text: heading, content: "<li>…"}` sections. Measured on Mobileye, 25 Israel-relevant postings: the description fields yielded a years-of-experience requirement on **0**, `lists` on **18**. So use `inline_field: lists` and record the section keys — `references/profile_schema.md` → "When the inline field is structured" covers the shape. A profile pointing at `descriptionPlain` here would pass every validation, carry a real `verified_on_job_url`, and still determine nothing.
2. **Static HTML of the posting page.** `web_fetch` one real posting URL taken from the listing you just pulled. If the requirements text appears in the raw markup, that's `method: html` — one plain request per *new* posting, no browser.
3. **Playwright.** Only if the posting page is a JS shell. Record `content_selector` and `wait_for`.
4. **Nothing workable.** Write `method: none` with a one-line reason. This is a legitimate result, not a failure.

Whichever branch you land on:

- **Try to isolate the requirements/qualifications section** and record `requirements_section_selector`. This is the single biggest accuracy win available here: parsed against a whole posting, a "5+ years preferred" line sitting in a *nice-to-have* list will disqualify a genuinely entry-level role. If no such section is identifiable, leave it `null` — the parser falls back to the full text, less precisely.
- **Record `verified_on_job_url`** — the actual posting you checked. Same rule as every other claim in this skill: a `detail_fetch` block without a URL it was confirmed against is a guess, and must not be written at all. Omitting the block is the correct output when you couldn't verify; the filter is fail-open and degrades to "undetermined" for that company, whereas a wrong selector silently withholds relevant jobs from the user.
- **Do not extend the location filter or pagination work to posting pages.** This step reads exactly one posting to learn the shape. At runtime the same access is applied only to postings that are new *and* survived the title pre-check.

## Step 4 — Confirm required rendering method

If Step 3 was needed at all, `fetch_type: playwright`. If Steps 1–2 resolved it, `api` or `html`. Don't reach for Playwright if a plain fetch worked — it's slower, heavier, and much more likely to be blocked at runtime (Step 8).

## Step 5 — The relevance filter (Israel + qualified remote)

**v2 changed the semantics.** The filter is no longer "is this in Israel" but "is this reachable from Israel":

A posting is **kept** if either:
- its location matches an Israeli place (see the normalized list and matching rules in `assets/function_templates.py` → `RELEVANCE_HELPER`), **or**
- its location indicates remote work (`remote`, `hybrid`, `anywhere`, `work from home`, `מרחוק`) **and does not name a foreign country/region/timezone**.

That second clause is the "qualified remote" rule: `Remote`, `Remote - EMEA`, `Remote - Global`, `Remote (Israel)` are kept; `Remote - US`, `Remote - Americas`, `Remote, EST hours`, `Remote - UK` are dropped. The exclusion list lives in one place in the helper and is unit-tested.

Matching must be done on a **normalized** string: lowercase, strip punctuation and hyphens, collapse whitespace. This is not cosmetic — Lever returns `"Tel-Aviv, Israel"` with a hyphen, so a naive substring check for `"Tel Aviv"` fails on it. Hebrew place names (`תל אביב`, `ישראל`, `הרצליה`…) are included in the keyword list.

**Where this filter runs depends on `israel_filter.method`:**
- `url_param` — the site already filtered; the helper still runs as a cheap sanity check, never as the primary mechanism.
- `ui_interaction` — same.
- `post_fetch` — the helper *is* the mechanism. This is the only acceptable case for fetching everything and filtering afterward, and it applies to every `fetch_type: api` profile by definition (the API returns all postings with their locations; filtering its JSON is a one-line check, and there is never a reason to automate a location dropdown for an API-type profile).

**Known gap to handle per-platform, not in the helper:** Greenhouse frequently reports `location.name` as `"Multiple Locations"`, which no keyword check can resolve. For Greenhouse, always request `?content=true` and check the `offices` array as well as `location.name`; a posting whose `offices` include an Israeli office is kept even if `location.name` says "Multiple Locations". Record in the profile which fields you actually checked.

## Step 6 — Write the profile

Write `profiles/<company_slug>.json` following `references/profile_schema.md`. **For the
job-alert project, this is the entire deliverable** — the profile *is* the registration, and
`src/fetchers/` already implements every template below generically, dispatching on
`fetch_type` / `pagination.method` / `israel_filter.structure` read straight from the file. Do
not paste a per-company function into that project; there is nowhere for it to go, and a
hundred companies would mean a hundred dead copies of code that already exists once.

The templates in `assets/function_templates.py` are therefore **a specification you are
filling in, and a standalone artifact for use outside that project**. Read the matching one to
know exactly which fields your profile has to supply and what the fetcher will do with them;
only hand over generated code if the user is not using the profile-driven project:

| Condition | Template |
|---|---|
| `fetch_type: api` | the per-platform API template (`TEMPLATE_API_LEVER`, `_GREENHOUSE`, `_COMEET`, `_SMARTRECRUITERS`, `_ASHBY`) |
| `fetch_type: html` | `TEMPLATE_HTML_STATIC` (or `TEMPLATE_HTML_JSONLD` if Step 2 found JSON-LD) |
| `playwright`, pagination `none` | `TEMPLATE_PLAYWRIGHT_SINGLE_PAGE` |
| `playwright`, pagination `url_param` | `TEMPLATE_PLAYWRIGHT_URL_PAGES` |
| `playwright`, pagination `click` | `TEMPLATE_PLAYWRIGHT_CLICK_NEXT` |
| `playwright`, pagination `scroll` | `TEMPLATE_PLAYWRIGHT_INFINITE_SCROLL` |
| `playwright`, `israel_filter.structure = flat_multi_location` | `TEMPLATE_PLAYWRIGHT_MULTI_LOCATION` (overrides the above — it owns its own per-city pagination) |

**Before picking any `playwright` template, re-check Steps 1a and 1b.** If a known ATS API exists, use the API template regardless of how the site's UI happens to filter by location. The `flat_multi_location` template exists for sites that genuinely have no API and happen to use that picker — not as the default whenever a multi-city picker is seen.

Every `playwright` template has an `{{RELEVANCE_FILTER_ACTIONS}}` placeholder. In the
profile-driven project the equivalent is `israel_filter.ui_actions_structured`:
- `method = url_param`: leave it as a comment — the URL is pre-filtered, nothing to do at runtime.
- `method = ui_interaction`: fill it with the exact click/select/wait sequence from Step 3a, so the filter is applied before any job cards are read. In `TEMPLATE_PLAYWRIGHT_URL_PAGES` this goes inside `apply_relevance_filter(page)`, which the loop calls after every navigation.
- `method = post_fetch`: leave it empty; the `is_relevant_location(...)` check inside the loop does the work.

**`fetch_type: api` is limited to `lever`, `greenhouse`, `comeet` and `smartrecruiters`** — the
four with a live-verified handler. The profile loader rejects any other value, so a confirmed
Ashby/Workable/Recruitee/Workday board means writing the handler first, not writing the profile
and hoping.

Fill the remaining placeholders (`{{BASE_URL}}`, `{{JOB_SELECTOR}}`, `{{TITLE_SELECTOR}}`, `{{LOCATION_SELECTOR}}`, `{{LINK_SELECTOR}}`, `{{NEXT_BUTTON_SELECTOR}}`, `{{PAGE_PARAM}}`, `{{MAX_PAGES}}`…) with what you verified. **Every placeholder must come from something observed on the site.** `{{LINK_SELECTOR}}` is mandatory for html/playwright profiles — a function that can't return a per-job URL is not finished.

## Step 7 — Health semantics (new in v2, and not optional)

### Step 7.0 — Completeness gate: is this the *right* board? (new in v3)

Before recording any health numbers, stop and ask whether the count you're about to write is **plausible for a company this size**. Everything else in this skill verifies *correctness* — that the selector works, that the pagination terminates, that the filter applies. Nothing until now verifies **completeness**, and a wrong-but-working source passes every earlier check silently.

This is a real, observed failure, not a hypothetical: Wiz has a Tel Aviv R&D centre and 1000+ employees, and the Greenhouse board token `wizprivate` was in use in this project. That board returns HTTP 200, valid JSON, and two genuine postings — London and New York. After the Israel filter: **zero**. Every check in Steps 1–6 passes. A separate token, `wizinc`, appears as a Greenhouse embed on the company's own careers page, and third-party aggregators list well over a hundred Wiz postings. The board wasn't broken; it was the wrong board.

**The gate.** If, after the relevance filter, the count is `0` — or implausibly small next to the company's headcount and known Israeli presence — do **not** write the profile and do **not** set `zero_is_plausible: true` to make it go away. Treat it as a signal that Step 0 or Step 1 landed on the wrong source, and go back:

1. Re-run Step 1b against the company's **own** careers domain, even if Step 1a already produced a working ATS URL from a search result. An embedded `job_board?for=<token>` / Lever / Comeet widget on the branded page is the company's *canonical* board; a token found by search may be a secondary, regional, or legacy one.
2. Check explicitly for **more than one board under the same platform**. Companies run separate boards for private/confidential roles, for acquisitions, for specific regions. Finding one working token is not evidence it's the only one, and never evidence it's the main one.
3. Cross-check the magnitude against an independent source — an aggregator listing, or the company's own "N open positions" counter. You are not scraping it, only sanity-checking an order of magnitude. A board showing 2 postings for a company that advertises 150 is the wrong board.
4. If after all this the count is genuinely zero, say so plainly in `notes` **with the evidence** ("company careers page itself shows no Israeli openings"), and only then set `zero_is_plausible: true`.

`zero_is_plausible: true` is a claim that a company genuinely isn't hiring in Israel. It is not a way to silence an inconvenient result, and it disables the single mechanism that would otherwise catch this later.

### Step 7.1 — Failure semantics

A profile isn't done until it answers: **how does the caller tell "0 jobs because nothing is open" apart from "0 jobs because the site changed"?**

This matters more than anything else in the profile. If a selector breaks, the function returns an empty list, the diff sees no new jobs, and the bot goes quiet **forever** while appearing perfectly healthy. That is the worst failure mode this system has.

The runner has **three** checks, and only one of them wants a number from you:

| Check | Catches | Needs |
|---|---|---|
| count is `0` after a healthy run | a dead `job_selector` | nothing |
| count below `expected_min_jobs`, from a run above it | **slow decay** — a drift downwards over weeks never trips a run-to-run comparison | this field |
| count below 40% of the last healthy run | **sudden breakage** — broken pagination, page 1 parses and pages 2..N stop | nothing |

Record in the profile's `health` block:

- `expected_min_jobs` — **set this lazily. Roughly half the count you observed, rounded down. Do not deliberate over it.**

  That instruction is not a shortcut, it is the correct rule, and the temptation to ignore it is the reason it is written this way. Careful reasoning about this number pulls it *upwards*, toward the count you just measured, because a tight number feels more rigorous. It isn't — it is worse. A floor near the observed count fires on ordinary week-to-week churn, and every false fire freezes new-job detection for that company for three runs. The floor's only job is catching a slow slide that the 40% ratio check structurally cannot see, and a lazy number does that just as well as an agonised one.

  Half of what you saw, then move on. `0` is the one value to avoid unless the company genuinely has nothing (it disables the check). Mobileye records 20 against 122 observed; anything in that spirit is right.
- `zero_is_plausible` — `true` only for genuinely small companies where zero Israeli openings is a normal state. Default `false`. Note it disables **all three** checks above.
- `sentinel` — some element that must exist on a healthy page regardless of results (a "0 results" empty-state message, a total counter, a filter control). **Documentation only — no code reads it today.** Record it anyway: it is what tells whoever rebuilds this profile how to distinguish a rendered-but-empty page from one that never rendered.

Also record what you know about runtime fragility:
- `requires_browser: true` profiles should note that GitHub Actions IP ranges are frequently blocked by Cloudflare / Akamai / PerimeterX on careers sites. **A site that loaded fine in claude-in-chrome may still return 403 from a runner.** This cannot be verified from here — write it as a known unknown in `notes` and expect the first real run to be the actual test.
- Note any rate limiting or obvious bot-detection you noticed during investigation.

## Step 8 — Output

Give the user:
1. The resolved careers URL from Step 0, if it wasn't the obvious guess (it redirected, or search took a couple of tries) — worth surfacing so the user can eyeball it.
2. The profile JSON, ready to save as `profiles/<slug>.json`. **This is the deliverable.**
3. **Not** a generated Python function, and **not** a `companies.json` line — unless the user is working outside the profile-driven project. `src/fetchers/` already implements every template generically and dispatches on fields read from the profile, so per-company code has nowhere to live. If the profile needs a combination the dispatcher doesn't yet handle, say that explicitly — that's a dispatcher change, not a new hardcoded type string.
4. A reminder that a new profile needs **seeding** before its first normal run (`python run.py --seed`), or its entire existing job list arrives as one alert.
5. Whether a `detail_fetch` block was produced, and if not, why — the user needs to know that the experience filter will pass everything through for this company tagged as undetermined, rather than discovering it from the alerts.
6. A short, honest list of what you could **not** verify — unreached last pages, untested clamping behavior, an API endpoint from the UNVERIFIED rows you couldn't confirm live, a selector located visually but never pinned to a literal class. Say it plainly rather than smoothing it over.

Don't invent behavior for edge cases you didn't observe.
