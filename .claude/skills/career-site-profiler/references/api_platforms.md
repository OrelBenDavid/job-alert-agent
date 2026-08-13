# Known ATS API platforms — field reference (v2)

Use this when Step 1a (URL pattern) or Step 1b (HTML signature) of SKILL.md matches.
No browser automation needed for any of these.

**Every entry lists an id field and a url field.** In v2 these are mandatory: the id drives
new-job detection, the url goes into the Telegram alert. An adapter that returns neither is
not finished.

**Verification status is stated per platform.** The four marked UNVERIFIED came from pattern
knowledge, not from a live call made while writing this file. Do not copy an unverified
endpoint into a profile — make the call, read the actual response, and correct the field
names against what you see.

---

## Lever — verified

- URL: `jobs.lever.co/<slug>` (US) or `jobs.eu.lever.co/<slug>` (EU)
- API: `GET https://api.lever.co/v0/postings/<slug>?mode=json`
- **The `.eu.` subdomain matters.** If the board URL was `jobs.eu.lever.co`, the API is
  `api.eu.lever.co` — the non-EU host returns 404 for that slug. Confirmed with Mobileye.
- No auth. Returns a **JSON array directly**, not wrapped in an object.
- Fields: `id` → job id · `text` → title · `categories.location` → location · `hostedUrl` → url
- All postings in one call, no pagination.
- Lever's own UI uses a single-select "City, Country" location picker with no aggregate
  "Israel" option (`flat_multi_location`). **Ignore it.** The API returns every posting's
  location anyway; filtering is a one-line post-fetch check. Never automate that picker for
  a company that resolved to Lever.

## Greenhouse — verified

- URL: `job-boards.greenhouse.io/<token>` or `boards.greenhouse.io/<token>`
- Embedded form: look for `grnhse_app` or `boards.greenhouse.io/embed/job_board?for=<token>`
  in a branded page's HTML — the `for=` value is the token (Step 1b).
- API: `GET https://boards-api.greenhouse.io/v1/boards/<token>/jobs?content=true`
- No auth. Response is `{"jobs": [...]}`. All postings in one call.
- Fields: `id` · `title` · `location.name` · `absolute_url`
- **`?content=true` is required, not optional.** Without it there is no `offices` array, and
  Greenhouse very frequently reports `location.name` as `"Multiple Locations"` — which no
  keyword check can resolve, so Israeli roles get silently dropped. With it, check
  `offices[].name` as well and keep the posting if any office is Israeli. Record both fields
  in the profile's `israel_filter.checked_fields`.
- Cost of `content=true`: a much larger response (full HTML descriptions). Acceptable — it
  runs every 3 hours, not every second.

## Comeet — partially verified

- URL: `comeet.com` in the URL, or in the page source. Very common in Israeli companies, and
  **usually embedded as a widget on the company's own domain** — so Step 1b matters more here
  than Step 1a.
- API: `GET https://www.comeet.com/careers-api/2.0/company/<company_uid>/positions`
- The `company_uid` appears in the widget's script/iframe URL. Read page source or
  `read_network_requests` and look for a request to `comeet.com`.
- **UNVERIFIED detail: most embeds also pass a `?token=<token>` query param, and the call may
  fail without it.** I have not confirmed whether the endpoint works token-less. Capture both
  the uid and the token during investigation, try without the token first, and write down
  which one actually worked.
- Fields (per docs/pattern, not confirmed live): `uid` → id · `name` → title ·
  `location.name` → location · `url_comeet_hosted_page` (fallback `url_active_page`) → url
- Returns a JSON array of positions.

## SmartRecruiters — verified

- URL: `jobs.smartrecruiters.com/<company>`
- API: `GET https://api.smartrecruiters.com/v1/companies/<company>/postings`
- No auth for GET. Response is `{"content": [...], "totalFound": N}`.
- **This one IS paginated** — `offset` / `limit` query params, default page size ~20. Loop
  until `content` is empty or `offset >= totalFound`. Keep a hard page cap anyway.
- Fields: `id` (fallback `ref`) → id · `name` → title · `location.city` + `location.country`
  → location · url built as `https://jobs.smartrecruiters.com/<company>/<id>`

## Ashby — UNVERIFIED

Worth checking early: very common in Israeli startups founded in the last few years.

- URL: `jobs.ashbyhq.com/<slug>`
- Suspected API: `GET https://api.ashbyhq.com/posting-api/job-board/<slug>`
- Expected fields (**guessed, must be confirmed**): `id` · `title` · `location` ·
  `jobUrl` / `applyUrl`, under a `jobs` array.
- Ashby also exposes a GraphQL endpoint used by its own UI; if the posting-api call fails,
  `read_network_requests` on the board page will show the real request to copy.

## Workable — UNVERIFIED

- URL: `apply.workable.com/<slug>`
- Suspected API: `GET https://apply.workable.com/api/v1/widget/accounts/<slug>?details=true`
- Confirm the response shape before use.

## Recruitee — UNVERIFIED

- URL: `<slug>.recruitee.com`
- Suspected API: `GET https://<slug>.recruitee.com/api/offers/`
- Response expected as `{"offers": [...]}`. Confirm before use.

## Workday — UNVERIFIED, heavier, still cheaper than Playwright

- URL: `<company>.wd1|wd2|wd3|wd5.myworkdayjobs.com/<site>`
- It's a **POST**, not a GET:
  `POST https://<company>.wd3.myworkdayjobs.com/wday/cxs/<company>/<site>/jobs`
  with a JSON body along the lines of `{"limit": 20, "offset": 0, "searchText": "",
  "appliedFacets": {}}`.
- Paginated via `offset`; the response carries a total count.
- The location facet ids are opaque and site-specific — easier to fetch everything and filter
  post-fetch than to reverse-engineer the facet values.
- Used by several large employers with Israeli sites. If you hit a Workday board, get the
  exact body and response shape from `read_network_requests` on the real page rather than
  trusting this entry.

---

## What to do when none of these match

Go to Step 2 (plain fetch / JSON-LD), then Step 3 (browser). But **re-read Step 1b first** —
an embedded board on a branded domain is the single most common reason a company gets
wrongly classified as `playwright`, and Playwright is both the slowest path and the one most
likely to be blocked from a GitHub Actions runner.
