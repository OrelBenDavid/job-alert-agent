# Worked examples and known traps (v2)

Kept here as a model of what "verified, not guessed" looks like at each step — and, just as
importantly, as a record of the mistakes that were actually made.

---

## Worked example: Wix — pagination discovered the right way, filter never done

**Status: this trace is incomplete and the resulting profile must be redone under v2.**

The trace below was recorded before the skill required finding and applying the location
filter *before* investigating pagination. At the time, pagination was investigated on the
**unfiltered** list. Redoing it today, Step 3a comes first — and the result may well be
different: filtering to Israel-relevant roles could collapse "2 pages of everything" into a
single page, or turn out to be a URL param that removes the need for any pagination logic.

### Step 1 — known ATS?
URL is `careers.wix.com` — no match against the URL-pattern table. **Note: under v2 this is
not yet a conclusion.** Step 1b (grep the HTML for an embedded Greenhouse/Comeet/Lever/Ashby
widget) was never run on Wix. It should be, before accepting `fetch_type: playwright`.

### Step 2 — plain fetch
`web_fetch` returned a page shell — no job titles, just "Loading Team" / "Loading Location"
placeholders. Confirms JS rendering is required. (JSON-LD was not checked for; worth a look
under v2.)

### Step 3 — browser investigation
- Navigated with `claude-in-chrome:navigate` and screenshotted: the real UI has a keyword
  search box, a Profession filter, a Type filter, and a Location dropdown — all filtering one
  flat list, not separate category pages.
- **First read was wrong.** Scrolling fast to the footer produced an "infinite scroll"
  conclusion. That was lazy-loading *within* a single page being mistaken for pagination, and
  it was only caught because the user pointed at the page and said to look again. Worth
  internalizing: a fast scroll is not an observation.
- `find` with the query "pagination showing X out of Y with Next/Previous buttons" located
  it: "Showing 1 of out 2" with a Next button.
- Clicked Next once, watched the tab URL: it changed to `?page=2`. Signal for URL-based
  pagination.
- **Verified rather than assumed**: navigated directly to `careers.wix.com/positions?page=2`
  with no prior click — different jobs appeared (Junior Accountant, Customer Care Expert),
  confirming the param alone drives content, independent of client-side click state.
- On page 2 the counter read "Showing 2 of out 2" and Next was disabled — 2 pages total.
- `read_network_requests` around load and scroll captured only analytics traffic (Facebook
  Pixel, LinkedIn Ads, Google Ads, Clarity) — no job-data API, so no shortcut around
  rendering for this site.

### What's still missing from this trace (all required by v2)
1. Step 1b HTML-signature check — never done.
2. Step 3a location filter — the Location dropdown exists but was never opened, so we don't
   know whether it's `url_param` or `ui_interaction`, nor its structure.
3. Pagination re-checked **on the filtered view**.
4. Out-of-range behavior — what does `?page=999` do? Empty list, or clamp back to page 1? If
   it clamps, a naive loop never terminates.
5. The literal CSS selectors. The job elements were located visually and by text, never
   pinned to real class names. The old trace itself flagged this and said "don't skip this
   step" — it then got skipped.
6. `link_selector` — mandatory in v2, never captured.
7. `health.expected_min_jobs` — never recorded.

---

## Worked example: Mobileye — trust the search results, not the branded domain

Short but important, because it catches a different failure mode than Wix.

- Company name given: "Mobileye". `web_search` for "Mobileye careers jobs" returns several
  results, including `careers.mobileye.com/jobs` (branded, genuinely presents itself as an
  "Open Positions" page) **and**, separately, `jobs.eu.lever.co/mobileye`.
- The trap: the branded page looks like exactly the right answer — company's own domain,
  literally titled "open positions". Stopping there and concluding "not a known ATS, must
  need Playwright" would have been a real and costly mistake.
- The fix (Step 0.1): scan *all* search results against the ATS pattern table before settling
  on the branded domain. `jobs.eu.lever.co/mobileye` matches Lever directly.
- Confirmed by calling `https://api.eu.lever.co/v0/postings/mobileye?mode=json` — clean JSON,
  all postings in one call, no pagination, no browser.
- Resolved from search results alone: no landing-page fetch, no link-hunting, no browser.

---

## Noted pattern: hierarchical location filter (careers.mobileye.com/jobs)

**User-reported, not independently browser-verified.** Flagged as such rather than presented
as fact.

On the branded page (not the Lever board), clicking the Location filter and then a "+" next
to "Israel" reportedly does not filter — it expands Israel into individual cities/offices,
each with its own checkbox, and every one must be selected for full coverage. This matches
the "hierarchical" case in Step 3a. Whether it resolves to a bookmarkable URL or stays
JS-only should be verified normally before it enters any profile.

---

## Noted pattern: flat multi-location picker, no aggregate country option (Lever)

**Directly browser-verified** — navigated, clicked, screenshotted, checked the resulting URL.

On `jobs.eu.lever.co/mobileye`, the Location dropdown lists individual "City, Country" options
— `Beijing, China`, `Haifa, Israel`, `Jerusalem, Israel`, `Koblenz - Neuwied, Germany`,
`Petah Tikva, Israel`, `Ramat Gan, Israel`, `Shanghai, China`, `Tel-Aviv, Israel` — with **no
aggregate "Israel" option**. Selecting "Jerusalem, Israel" changed the URL to
`?location=Jerusalem%2C%20Israel` (URL-based, confirmed), and re-opening the dropdown showed a
single checkmark — selecting another city replaces it rather than adding. That's
`flat_multi_location`: full coverage needs one fetch per matching city, merged and
de-duplicated by job id.

**But this site never needs that loop.** Mobileye is `fetch_type: api` — the Lever API returns
every posting's `categories.location` in one call regardless of what the UI looks like. The
investigation was worth doing to understand the *pattern* for future API-less sites, but it
was never the path for Mobileye's real profile. **Always re-check Step 1 before building
browser automation for a location picker.**

Two v2 notes on this pattern, both real bugs found in the v1 template:
- `Tel-Aviv, Israel` contains a space and a comma. It must be `quote()`d before going into a
  URL. v1 interpolated it raw.
- Each city can have its own pagination. v1's template fetched only the first page per city
  and silently dropped the rest, despite the skill claiming it handled per-city pagination.

---

## Trap: `Tel-Aviv` vs `Tel Aviv`

Lever returns `"Tel-Aviv, Israel"` with a hyphen. A naive substring check for `"Tel Aviv"`
does not match it. On Mobileye this was masked by the `", Israel"` suffix also being present —
pure luck. A site that renders only the city name would have been silently dropped.

Hence the normalization step in `RELEVANCE_HELPER`: lowercase, delete apostrophes (so
`Be'er Sheva` → `beer sheva` and `Ra'anana` → `raanana`), turn remaining punctuation into
spaces, collapse whitespace, and match whole words with padding. The apostrophe rule is
specifically *delete*, not *replace with space* — replacing splits `Be'er` into `be er` and
misses the keyword. This was caught by running the test cases, not by reading the code.

Whole-word matching also matters in the other direction: matching `us` as a substring would
flag `Austin`, and `est` would flag `West Palm Beach`.

---

## Trap: pagination that clamps instead of ending

Many careers sites serve page 1's content for an out-of-range `?page=999` rather than an empty
list. Against a `while True` loop that only breaks on "no cards", that is an infinite loop —
in a GitHub Actions context it burns until the 6-hour job timeout.

Always test an out-of-range page number explicitly and record the behavior in
`pagination.out_of_range_behavior`. The v2 template additionally carries a hard `MAX_PAGES`
cap and compares a fingerprint of each page's job ids against the previous page, but the
profile should still document what the site actually does.

---

## Trap: "Next" that isn't `disabled`

Playwright's `is_disabled()` checks the form `disabled` property. Careers sites overwhelmingly
express a dead "Next" as `<a class="disabled">` or `aria-disabled="true"`, for which
`is_disabled()` returns `False` — producing an endless click loop on the last page. Record the
actual disabled marker you observed in `pagination.disabled_marker`; the v2 template checks
aria, class, visibility, and whether the click produced any new job ids at all.

---

## Trap: the silent zero

The most dangerous failure this system has, and the reason for the `health` block.

If a selector breaks after a redesign, the fetch function returns an empty list. The diff sees
no new jobs. Nothing is sent. **The bot goes quiet forever while looking perfectly healthy**,
and the user concludes nobody is hiring.

So every profile records `health.expected_min_jobs` (the count observed at profiling time) and
`health.zero_is_plausible` (default `false`). A drop from a healthy count to zero is treated
as a failure: saved state is **not** overwritten, and a maintenance alert is raised. A
`sentinel` selector — something that exists on a healthy page even with zero results, like an
empty-state message or a result counter — makes this even more reliable.

---

## Trap: it worked in the browser, it 403s from the runner

claude-in-chrome runs from a normal-looking browser. GitHub Actions runs from a datacenter IP
range that Cloudflare, Akamai and PerimeterX frequently challenge or block on careers sites.

**This cannot be verified from the investigation session.** Don't claim a `playwright` profile
works in production; write it in `notes` as a known unknown and treat the first scheduled run
as the actual test. This is one more reason the cheap-to-expensive order in Step 1 → Step 2 →
Step 3 is about reliability, not just speed.
