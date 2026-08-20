# Expansion strategy: from 256 companies to Israel-wide coverage

Written 2026-08-19. Every number marked **verified** was measured with a live
request made while writing this document; every number marked *(est.)* is a
projection from a verified measurement and is labelled as such.

The goal this document plans for: **the bot alerts on any company in Israel
that could plausibly offer a target-family job and has a careers page.**

---

## 0. The headline

The project's own conclusion after the 2026-08-18 sweep was correct but
incomplete:

> "The seed CSV is now exhausted. Growing past 233 is a sourcing problem, not a
> scraping one: another pass over the same file cannot help."

It is a sourcing problem - but not because the source list is too short. It is
because **discovery is running in the wrong direction.** Today it goes
`company name -> guess a slug -> probe 7 platforms -> prove identity`. That
direction is expensive (288,000 requests for 88 companies) and structurally
lossy (a third of raw hits were the wrong company; `Viz.ai` was unguessable).

The inverse direction - **enumerate the ATS tenants first, then ask which are
Israeli** - is cheaper by three orders of magnitude and has no identity problem
at all, because the tenant id *is* the identifier the fetcher needs.

Measured cost per company gained:

| Approach | Requests | Companies gained | Requests per company |
|---|---:|---:|---:|
| Current sweep (name -> slug) | 288,000 | 88 | **3,272** |
| Common Crawl -> Comeet probe | ~550 *(est.)* | 224 candidates -> **~90 live Israeli boards** *(sampled)* | **~6** |
| Workable aggregator feed | 8 **verified** | **38 verified** | **0.2** |

But this only holds on an **Israel-dense platform**. The same query against
Greenhouse yields 4,295 tenants and ~43 Israeli boards - see A1's yield table,
which is the single most decision-relevant measurement here.

Both candidate sets are written out and ready to import:
`_onboarding/comeet_candidates.csv` and `_onboarding/workable_candidates.csv`.

---

## 1. Where the ceiling actually is

Four hard numbers from this repo, all of which shape everything below.

**Yield is size-dependent and the curve is brutal.** On-family postings found
per 100 companies probed, from the 2026-08-18 sweep:

| Size bucket | On-family per 100 probed | Rows in seed |
|---|---:|---:|
| `l` | 86 | 445 |
| `xl` | 54 | 101 |
| `m` | 15 | 862 |
| `s` | 4 | 2,394 |
| `xs` | **0.7** | 4,139 |

The `xs` bucket cost 4,138 probes for 29 postings. **Any strategy that probes
an unranked list is spending 52% of its budget on the bucket that returns
0.7%.** Ranking before probing is not an optimisation, it is the difference
between a viable sweep and a wasted one.

**Platform concentration.** 108 of 256 companies are Comeet - an Israeli
vendor, and by far the highest-density Israeli ATS. Greenhouse is 82. The two
together are 74% of the corpus. Whatever else is done, **Comeet enumeration is
the single highest-value target.**

**The corpus is API-shaped and must stay that way.** 255 of 256 companies are
`fetch_type: api`. The one `playwright` company (Wix) costs ~30s against
~0.4-1s for an API company, and the browser pool is 2 wide. See section 9 for
why this is the binding constraint on scale, not cost.

**Three constants in the code cap growth before any network limit does.**
See section 10 - they are the first thing to fix, and they are cheap fixes.

---

## 2. Strategy A - Invert discovery: enumerate tenants, not companies

The core strategy. Two independent sources, both free, both verified live.

### A1. Common Crawl's URL index

Common Crawl publishes a CDX index of every URL it has crawled, queryable over
HTTP with no key and no rate limit. ATS boards live on a handful of known
hostnames, so a prefix query enumerates tenants directly.

**Verified 2026-08-19 against `CC-MAIN-2026-30` (July 2026 crawl):**

| Query | Records | Distinct tenants |
|---|---:|---:|
| `job-boards.greenhouse.io/*` | 65,008 | **4,295 board tokens** |
| `www.comeet.com/jobs/*` | 715 | **185 slug/uid pairs** |
| same, union over 4 crawls | - | **270 pairs / 264 uids** |

**Across the 4-crawl union, 224 of the 270 Comeet slug/uid pairs are not in
`profiles/companies/`** - written to `_onboarding/comeet_candidates.csv` with an
`already_profiled` column. Spot-checked names in the new set: `cyera`,
`akeyless`, `augmedics`, `bizzabo`, `buildots`, `blockaid`, `deepdub`,
`docontrol`, `elementor`, `dailydev`. These are exactly the calibre of company
the bot exists for.

For scale: the repo currently has **108** Comeet companies, and this one free
query proposes **224 more** - before any other platform is touched.

Why this is better than guessing, not just cheaper:

- **Identity is free.** The repo already established that for Comeet "the uid is
  the authoritative half of the pair" - a wrong slug with a right uid yields no
  token. Common Crawl hands over the uid *as published by the company's own
  page*. There is no name-matching step, so there is no wrong-company class of
  error. The `A-LIGN External` failure mode cannot occur.
- **Unguessable slugs are included.** `Viz.ai` - capital V, literal dot - is a
  URL like any other to a crawler.
- **Zero load on the ATS during discovery.** The expensive, throttle-triggering
  part of the current sweep (thousands of speculative 404s against
  `apply.workable.com`) disappears entirely. Probing happens once, against a
  list already known to exist.
- **Multi-crawl union is a real gain.** Comeet went 185 -> 270 across four
  crawls (+46%). Crawls back several years are indexed; older ones add dead
  tenants, which the probe step removes for one request each.

Cost: a full pull of one host from one crawl is 1-5 HTTP requests returning a
few MB of JSONL. The whole enumeration across all seven known ATS hosts and
eight crawls is **well under 300 requests**.

**Caveat 1: coverage is a sample, not a census.** Only 31 of the repo's own 108
Comeet uids appeared in the July crawl. CC is an *additive* source, never a
replacement for what is already profiled, and its absence of a tenant proves
nothing.

### A1's yield depends entirely on whether the platform is Israel-dense

**This is the most important measurement in the document, and it was measured
after the first draft got it wrong.** Both sweeps were sampled live 2026-08-19,
scoring with `src/relevance.py` rather than a probe-local approximation:

| Platform | Sample | Boards with a **physical Israel** posting | Projected over the full tenant list |
|---|---:|---:|---:|
| **Comeet** (Israeli vendor) | 40 of the 224 new | **16 - 40%** | **~90 live Israeli boards** |
| **Greenhouse** (global vendor) | 200 of 4,295 | **2 - 1.0%** | **~43**, against 82 already profiled |

Named Comeet hits from the sample: `autobrains`, `nayax`, `overwolf`,
`blockaid`, `uveye`, `blinkops`, `qedma`, `flare`, `askai`. 85% of sampled
candidates resolved to a live board at all.

**The principle: invert discovery only where the tenant population is already
Israel-dense.** Comeet's tenant list is 40% useful because Comeet is an Israeli
vendor. Greenhouse's is 1% useful because Greenhouse is global - filtering 4,295
worldwide tenants to find ~43 Israeli boards, most of which are already
profiled, is *worse* than starting from a ranked Israeli company list
(Strategy B). **Do not run a blanket sweep against a global ATS.**

**Caveat 2, and it is a live policy question.** A further **15% of Greenhouse
boards (30 of 200) had no Israeli posting but did have bare `Remote`**, which
`relevance.py` deliberately keeps as qualified remote. That rule is right at 256
companies. Across a global tenant list it would admit ~640 mostly-US boards
whose "Remote" means "remote within the US". Any expansion onto global platforms
needs that rule revisited first, or the bot fills with roles nobody here can
take.

### A2. Public ATS aggregator feeds with a country filter

Some ATS vendors run a public job-search site across all their customers. Where
one exists with a location filter, it is a complete Israeli tenant list for that
platform, for a handful of requests.

**Verified 2026-08-19 - `jobs.workable.com/api/v1/jobs?location=Israel`:**

- HTTP 200, `application/json`, `totalSize: 141`
- 20 jobs per page, cursor pagination via `nextPageToken`
- Full walk: **8 requests, 141 postings, 38 distinct companies**
- Each record carries `company.title`, **`company.website`**, `location.{city,
  subregion, countryName}`, `locations[]`, `workplace` (`on_site` / `remote` /
  `hybrid`), `department`, `employmentType`, `requirementsSection`, `created`,
  `updated`, `url`

All 38 are written to `_onboarding/workable_candidates.csv` with website,
Workable company id and posting counts. A sample: AccessFintech, Autofleet,
CloudShare, D-ID, Healthee, Humanz, Nuvei, Rayzone Group, Regatta, Risco Group,
TAT Technologies.

Quality is mixed and the gates in section 7 matter here - the same feed also
returns law firms and non-Israeli entities with one Israeli opening. That is
expected, and it is what Gate 3 and Gate 4 are for.

**The aggregator is a discovery source, not a fetcher.** Verified 2026-08-19:
`location=Israel` returns only records whose `location.countryName` is Israel,
so it shows a company's Israeli slice and nothing else - it would silently drop
exactly the qualified-remote roles (`Remote - EMEA`) that `relevance.py`
deliberately keeps. The per-company endpoint to build the adapter on is
**`https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true`**
(verified HTTP 200, 31 KB on `autofleet`), which returns that company's whole
board. `?details=true` is what makes `detail_fetch` inline, the way Comeet's
`&details=true` does - which keeps the Workable companies clear of the
`MAX_DETAIL_FETCHES_PER_RUN` cap.

**This settles an open question in the repo.** `discover_ats.py` records that
Workable "appeared to have near-zero Israeli market share... That measurement was
taken WHILE the sweep was throttling it... Its real share is unmeasured, not
zero." It is now measured: **38 companies, 141 live Israeli postings.**

It also hands over the one thing the current discovery has to work hardest for
- `company.website` - which makes identity verification trivial.

Status of the other vendors, probed 2026-08-19:

| Vendor | Aggregate search | Result |
|---|---|---|
| Workable | `jobs.workable.com/api/v1/jobs` | **HTTP 200, JSON, works** |
| Greenhouse | `my.greenhouse.io/jobs` | 302 -> `/jobs/search`, JS app, no public JSON found |
| SmartRecruiters | `api.smartrecruiters.com/v1/postings` | 404 - per-company only |
| Ashby | `jobs.ashbyhq.com/` | 404 root; GraphQL endpoint answers but is per-board |
| Comeet | `comeet.com/jobs` | 301, no public cross-customer search |
| Lever | - | none exists |

So A2 covers Workable outright and nothing else today; A1 covers the rest.
Re-probe the Greenhouse search app with a browser once - if it has a JSON
backend with a location facet, that is 4,295 tokens reduced to the Israeli
subset for free.

### A3. Paid source-code search - the commercial version of A1

If A1's crawl coverage proves too sparse, the same query shape is available
commercially against a fresher and denser index:

| Tool | What it does | Cost | Verdict |
|---|---|---|---|
| **PublicWWW** | regex/literal search over page *source* - `boards.greenhouse.io` on `.co.il` domains | from ~$50/mo | Best fit; exactly the A1 query, denser index |
| **BuiltWith** | technology-usage lists, filterable by TLD | ~$295/mo+ | Overpriced for one query shape |
| **Wappalyzer** | same, with lookup API | ~$150/mo+ | Same |
| **SecurityTrails / DNS enumeration** | subdomain discovery | varies | Wrong shape - ATS boards sit on the vendor's domain, not the company's |

**Recommendation: do not buy anything yet.** A1 is free and has not been
exhausted. Revisit only if the multi-crawl union plateaus below expectations.

---

## 3. Strategy B - Widen the Israeli universe (top-down sourcing)

Strategy A finds companies that already sit on a known ATS. It cannot find a
company whose careers page is self-hosted and never crawled onto an ATS domain.
For those, a company list is still needed - and the 7,943-row seed is the small
end of what is available.

Ranked by usefulness, given the size curve in section 1:

**1. Startup Nation Finder - the best-fit source.** ~13,000+ Israeli tech
companies, investors and corporates, with sector, size and funding metadata.
That metadata is what makes it valuable: it lets the sweep be **ranked before
probing**, which section 1 shows is worth more than the extra rows. Access is
via the web app; there is no public export documented, so this needs either a
partnership request or a polite crawl of the search UI. Treat the result as a
**replacement** for the current seed's ranking columns, not just an append.

**2. Israeli job aggregators, mined for employer names.** AllJobs, Drushim,
JobMaster, Ethosia, SQLink. These carry the employers the tech-ecosystem
databases miss entirely - banks, insurers, healthcare, defence, industry,
municipalities - which is precisely the tail between "256 tech companies" and
"any company in Israel". Mine them for the *employer name*, then run the access
ladder in section 6. Do not mine them for the jobs themselves: agency listings
are duplicated, anonymised and stale, and would poison the corpus.

**3. Crunchbase / IVC Research Center.** Good funding and headcount signal for
ranking. Crunchbase has a paid API; IVC is subscription. Optional.

**4. `data.gov.il` - the Registrar of Companies (`ica_companies`).** **Verified
2026-08-19: 729,773 rows.** This is the theoretical ceiling - every registered
Israeli legal entity. It is also almost entirely noise: the overwhelming
majority have no website, let alone a careers page. **Do not use it as a probe
list.** Use it for the two narrow jobs it is genuinely good at: resolving a
legal name to a trade name, and confirming an entity is Israeli-registered when
a board's location data is ambiguous.

**Sourcing rule that falls out of this:** every new source must arrive with a
size or funding signal attached, or it cannot be ranked - and an unranked list
spends its budget in the `xs` bucket.

---

## 4. Strategy C - Platform breadth: each adapter is a multiplier

Seven platforms are implemented. Every additional ATS adapter unlocks its whole
Israeli tenant base at once - one adapter, then N companies at zero marginal
code, exactly as the `_platforms/` design intends.

The repo's own deferred list plus what A1/A2 can now measure:

| Platform | Status | Known Israeli signal | Priority |
|---|---|---|---|
| **Workable** | no adapter | **38 companies, 141 postings (verified)** | **1 - highest, and the feed doubles as the discovery source** |
| **Recruitee** | no adapter | 2 in the sweep; probe-able, A1 can size it | 2 - cheap JSON API |
| **Niloosoft / Hunter** | not investigated | Israeli vendor, heavy in non-tech and enterprise | 2 - the key to the non-startup tail |
| **Teamtailor** | no adapter | unmeasured | 3 |
| **BambooHR** | no adapter | Cyberbit; small boards | 3 |
| **SAP SuccessFactors / Oracle / iCIMS** | no adapter | large enterprises, multinationals' Israeli sites | 3 - high effort, high value per hit |
| **RippleHire** | no adapter | CyberProof only | 4 - skip |

**Sequencing rule: measure share before writing an adapter.** A1 gives a tenant
count per platform for a few requests; that number, filtered to Israel, is the
adapter's payoff. Write adapters in descending order of measured payoff, never
in order of how interesting the API looks.

---

## 5. Strategy D - The self-hosted tail

The last mile of "any company with a careers page". No ATS, no tenant to
enumerate. Three generic adapters cover most of it, in descending order of
reliability:

**D1. `schema.org/JobPosting` JSON-LD.** Any careers page that wants to appear
in Google Jobs emits structured JSON-LD with title, location (`jobLocation`,
including `addressCountry`), `datePosted` and `validThrough`. This is a
**standard**, so one generic fetcher covers every site that uses it - and the
country field is exactly the structured signal `relevance.py` already prefers
over free text (the same reason `location.country` beat `location.name` for
Comeet). This is the highest-value new fetcher in the whole plan after
Workable.

**D2. Sitemap mining.** `/sitemap.xml` -> URLs matching `/careers/`, `/jobs/`,
`/position/`. Gives a job-URL list and change dates without rendering anything.
Works where D1 does not; weaker, because the title and location must then be
parsed out of the page.

**D3. Feeds.** Some self-hosted boards still publish RSS/Atom or a JSON endpoint
discoverable in the page's network calls. This is what the existing
`career-site-profiler` skill already does well, one company at a time.

**D4. Browser automation - the last resort, and strictly rationed.** Section 9
shows the browser pool caps out around 40 companies for the whole corpus.
Playwright is for companies that are individually worth a slot (a Wix-scale
employer), not a fallback for the tail. **If a company can only be read by a
browser and is not large, leave it out** and record why - the project already
has that discipline.

---

## 6. The access-point ladder - one company, seven steps

For a single company name, in order, stopping at the first success. This extends
what `fingerprint_careers_page` already does, and orders the steps by cost:

1. **Tenant index lookup (new, free).** Is the company already in the A1 Common
   Crawl tenant set, or the A2 Workable feed? If yes, the identifier is already
   known and verified. *Cost: 0 requests.*
2. **Fetch the company's own careers page and fingerprint it.** The existing
   `_FINGERPRINTS` regex set against the raw HTML. Exact, one request, and the
   only route to Comeet and Workday. *Cost: 1 request.*
3. **Fetch the careers page's linked/embedded resources.** Iframes, `embed=js`
   scripts, WordPress plugin configs - the Insightec and Viz.ai cases. *Cost:
   1-3 requests.*
4. **Read the page's network calls in a browser.** The HiBob case: the source
   carried stale Comeet class names and only the browser saw the real
   `/api/job-ad` endpoint. *Cost: ~30s, ration it.*
5. **JSON-LD / sitemap / feed detection** (Strategy D). *Cost: 1-2 requests.*
6. **Slug guessing** - the current method, demoted to sixth. It is the only one
   that can produce a wrong company, so it runs last and only with the full
   `confidence_for` identity proof. *Cost: ~7 requests per platform.*
7. **Give up and record why.** A written-down exclusion is a result. The
   `dead_rows_reresolution.md` discipline should extend to the whole sweep.

**The reordering is the point.** Steps 1-3 are exact and cost 1-4 requests;
step 6 is inexact and costs dozens. Today the sweep effectively runs step 6
first.

---

## 7. Verification - is it legit, and does it contribute?

Five gates, each answering a different failure. A candidate must clear all five
to become a profile. The first three already exist in some form; gates 4 and 5
are the ones that stop the corpus filling with dead weight.

**Gate 1 - Identity.** Does this board belong to this company?

- *Free pass:* the company published the identifier on its own domain (ladder
  steps 1-3). Nothing to prove.
- *Otherwise:* `confidence_for` - match the board's own published name, and
  report `unverifiable` rather than `verified` where the platform publishes
  none. The existing rule stands: **`unverifiable` is not importable.** The 14
  deferred candidates were deferred correctly.
- *Plus:* `looks_like_demo` - vendor sample boards, which no name check catches
  (Recruitee's `google` board really is named "Google").

**Gate 2 - Liveness.** Does the endpoint answer with a parseable board today? A
dead token 404s (Greenhouse); a rotated Comeet token returns HTTP 400. One
request. Record the date, as every profile already does.

**Gate 3 - Israel-relevance.** Use `src/relevance.is_relevant_location`, not a
probe-local regex, and read structured fields where they exist. **The sweep's
own numbers were wrong for exactly this reason** - it predicted +606 on-family
and delivered +391, because `probe_greenhouse` joined `location.name` and
`offices[]` into one string while the fetcher treats `offices[]` as a fallback.
**The probe must import the fetcher's logic, not approximate it.** Otherwise
every future forecast is inflated the same way.

**Gate 4 - Contribution (the new one).** Does this company add anything the bot
would ever send? Run `roles.classify` over the live board and require at least
one Israel-relevant, on-family posting - or a credible reason to expect one. The
2026-08-18 sweep found 342 reachable boards and correctly imported only the 117
eligible: **225 were structurally dead** (175 had postings but none in Israel,
39 were empty, 11 were Israeli but entirely off-family). Importing those would
have cost 225 fetches per run, eight times a day, forever, to deliver nothing.

*The exception that must stay:* a board that is genuinely empty *today* but
belongs to a real Israeli employer is worth keeping, with `expected_min_jobs: 0`
and `zero_is_plausible: true` - the Wiz profile is the worked example.
**Contribution means "would ever contribute", not "contributes this minute".**

**Gate 5 - Stability.** Probe twice, at least 24h apart, before importing. This
catches the transient cases that would otherwise arrive as immediate maintenance
alerts: a board mid-migration, a throttled response, a CDN blip. Cheap, and it
protects the health gate's signal-to-noise - which is the thing that makes the
whole system trustworthy at 2,000 companies.

**A corpus-level check to run after every import batch:** postings per company,
alerts per company per month, and the count of companies that have never
delivered a single alert. The Comeet location bug was found because four
companies had never delivered anything - that metric is a working detector and
should be a standing report, not an incidental discovery.

---

## 8. Optimisation

### 8.1 The dataset

- **Rank before probing, always.** Size, funding, headcount - whatever the
  source provides. Probe `l`/`xl` exhaustively, `m` selectively, `s`/`xs` only
  through Strategy A (where they cost ~2 requests, not ~37).
- **Store the negative results.** The 225 structurally-dead boards and the
  ~7,400 no-hit companies are expensive knowledge. A `_onboarding/probed.csv`
  keyed by company with the date and outcome makes the next sweep incremental
  instead of a repeat. **This is the single cheapest optimisation in the
  document** and it is currently missing.
- **De-duplicate on identifier, not name.** The seed already carries the same
  company twice under two names (`gongio`). The board identifier is the real key.

### 8.2 The probing

- **Concurrency with per-host politeness.** The current sweep's one HTTP 429 in
  288,000 requests shows the network shape is fine; the Workable throttling
  shows what happens when one host takes thousands of speculative 404s. Rate
  limit **per hostname**, not globally.
- **Never brute-force a platform that throttles.** Workable is now reachable
  through its own feed (A2). `comeet` and `workday` stay non-guessable for the
  reasons already recorded.
- **Cache the Comeet board pages.** They are ~750 KB each and the token they
  carry effectively never changes. Resolve once at import, as the platform
  profile already documents - and now also cache during discovery, since A1 will
  be resolving ~270 of them.

### 8.3 The fetching (runtime, per run)

- **Conditional requests.** Most boards are unchanged between runs three hours
  apart. `If-None-Match` / `If-Modified-Since` where the ATS honours it turns a
  full board download into a 304. At 2,000 companies this is the difference
  between tens of MB and a few MB per run. Must be verified per platform before
  being trusted - a 304 on a board that *did* change would look like a healthy
  no-op, which is the silent-wrong-answer failure this project works hardest to
  avoid.
- **Tiered cadence.** Not every company needs checking every 3 hours. A board
  that has changed twice in six months does not. Tier by observed change
  frequency: hot (every run), warm (daily), cold (weekly). At 2,000 companies
  this cuts per-run work by ~60% *(est.)* with no loss of freshness where
  freshness matters.
- **Keep the corpus API-shaped.** See section 9.

### 8.4 Profile building

The `_platforms/` inheritance design is already the right answer and needs no
change - a new company on an existing platform is one thin JSON file and zero
code. Two additions:

- **Generate the thin record from the probe result.** `import_companies.py`
  already does this; extend it to consume A1/A2 output directly, so
  "enumerate -> verify -> import" is one pipeline rather than three.
- **Set health numbers from the live verified count**, as the importer already
  does, and re-verify them on a schedule. A stale `expected_min_jobs` produces a
  false maintenance alert, which is how the 2026-08-19 pass started.

### 8.5 State

**Verified:** 256 companies use ~320 KB of `state/seen` - **~1.2 KB per
company**. At 2,000 companies that is ~2.4 MB, rewritten and committed up to
eight times a day. Git will handle it, but the history will grow steadily.

Options, in order of preference: prune `seen` ids for postings that have
disappeared from a board for more than ~90 days (the id can never recur); if
that is not enough, move `state/` to an orphan branch so it does not bloat the
main history. **Do not** move state off the repo into a database until it
actually hurts - the current design's simplicity is worth real money.

---

## 9. Where and how to run it

### The constraint that actually decides this

Not cost. **Wall clock, and specifically the browser pool.**

From the repo's own measurements: api ~0.4-1s per company, playwright ~30s,
browser pool 2 wide, workflow timeout 20 minutes - and a run killed by that
timeout **never reaches the state commit**, so the entire run is lost.

Projected at 2,000 companies *(est., from those measurements)*:

| Corpus shape | Fetch wall clock | Verdict |
|---|---|---|
| 2,000 api @ 12 workers | ~2 min | fine |
| + 20 playwright @ 2 workers | +5 min | fine |
| + 40 playwright | +10 min | at the limit |
| + 100 playwright | **+25 min** | **exceeds the 20-min timeout - run lost** |

**So the hard cap is roughly 40 browser companies for the entire corpus,
regardless of hosting.** This is why Strategy D ranks JSON-LD above browser
automation, and why "any company in Israel" has to be reached through APIs and
structured data, not through rendering pages.

### Cost, at the projected scale

The repo is **private** (verified), so Actions minutes are metered: 2,000/month
free on the Free plan, $0.006/min beyond (published Linux 2-core rate).

Projected monthly minutes at 2,000 companies, 8 runs/day: ~2 min fixed overhead
+ ~2.5 min fetch = **~1,080 min/month** *(est.)* - still inside the free tier.
Cost is genuinely not the problem. Sharding across a job matrix would multiply
minutes (they sum across parallel jobs) and is the one thing that could push it
over.

### Platform comparison

| Platform | Free tier | Cost at this scale | Playwright | Scheduling | Python | Migration cost |
|---|---|---|---|---|---|---|
| **GitHub Actions - private (current)** | 2,000 min/mo | **$0** *(est. ~1,080 min)* | yes, 2 vCPU / 7 GB | cron in workflow | native | - |
| **GitHub Actions - public repo** | **unlimited** | **$0** | yes, **4 vCPU / 16 GB** | same | native | flip a setting |
| **Oracle Cloud Always Free** | 4 ARM cores / 24 GB VM, forever | **$0** | yes, comfortably | system cron | native | low - VM, cron, git push |
| **Hetzner CX22** | - | ~EUR 3.79/mo | yes | system cron | native | low |
| **Google Cloud Run Jobs** | generous free tier | ~$0-3/mo | yes (container) | Cloud Scheduler | native | medium |
| **Fly.io** | small free allowance | ~$2-5/mo | yes, needs RAM headroom | machine schedules | native | medium |
| **AWS Lambda** | 1M req + 400k GB-s/mo | ~$0-2/mo | painful (Chromium layer, 15-min cap) | EventBridge | native | medium-high |
| **Cloudflare Workers** | 100k req/day | $5/mo paid | **no** (Browser Rendering is paid and separate) | Cron Triggers | **no - JS/TS rewrite** | **high** |

### Recommendation

**1. Stay on GitHub Actions.** Nothing in the projection justifies a migration.
The workflow, the state commit and the secrets already work, and every
alternative trades that away for a saving of roughly zero.

**2. Consider making the repo public - the single highest-leverage infra
change.** It converts metered minutes to unlimited, and doubles the runner to
4 vCPU / 16 GB, which is the only clean way to raise `JOB_ALERT_BROWSER_WORKERS`
above 2 (the README is explicit that raising it on the current runner turns
healthy fetches into silent zeroes). The trade: the code, the profiles and the
state become public. Secrets stay secret - `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` are Actions secrets and are not exposed. **What does become
public is which companies are watched and every posting the bot has seen.** That
is a judgement call, not a technical one.

**3. Split the schedule before sharding the job.** If wall clock becomes tight,
run the hot tier every 3 hours and the cold tier once a day as a second
workflow. This cuts per-run time without multiplying billed minutes, which a job
matrix would.

**4. Move to Oracle Always Free or Hetzner only if the browser tier is forced
past ~40 companies.** A persistent VM removes the 20-minute cliff entirely and
lets a browser pool run 4-8 wide. It also means owning uptime, log retention and
the state commit - real ongoing cost in attention. Do not pay it speculatively.

---

## 10. Scaling blockers already in the code

Three constants will break before any network or cost limit does. All three are
cheap to fix and should be fixed **before** the next import batch.

**Status 2026-08-19: blocker 2 is fixed, 1 and 3 are still open.** Blocker 1 did
not need to block the Comeet and Workable imports - all three platforms serve
descriptions inline and cost that budget nothing - but it becomes urgent at
step 6, where a self-hosted board means one request per posting.

**1. `MAX_DETAIL_FETCHES_PER_RUN = 40`** - `src/detail.py:48`. The repo's own
note calls this "the single thing that stops this project scaling past a few
hundred companies": one company reworking its board spends the whole run's
allowance, and every posting past the cap stays `undetermined` and is delivered
untriaged. Comeet and Greenhouse now serve descriptions inline and are immune.
*Fix:* make the budget per-platform (uncapped for inline platforms, capped for
per-posting ones), or scale it with company count.

**2. `JOB_ALERT_MAX_NEW_JOBS = 50`** — **FIXED 2026-08-19.** It was calibrated
against "~1,350 Israel-relevant postings across 141 companies", where 50 was ~4%
of the corpus; at 2,000 companies that becomes ~0.3% and normal churn would trip
the gate on most runs. `state.py` now derives the threshold from the corpus each
run actually fetched (`IMPLAUSIBLE_NEW_JOBS_FRACTION = 50/1350`, floored at 50 so
it can only loosen), and `check.yml` no longer pins the variable. Loosening does
not blunt the gate: a full state reset reports ~100% of the corpus against a
threshold of ~3.7% of it, still caught by a factor of 27 at any size.

**3. `JOB_ALERT_FETCH_BUDGET_SECONDS = 780`** against `timeout-minutes: 20`.
Sound today. Re-derive both once the corpus and the tier split are settled - and
keep the property that matters: **finishing early with companies skipped is
always better than being killed before the state commit.**

One more, outside the code: **Telegram rate limits** (~20 messages/minute to a
single chat for bots). At 2,000 companies a genuine hiring burst can exceed
that. Batch multiple postings per message, and make the flood gate's held-alert
path the normal case rather than the emergency.

---

## 11. Recommended sequence

Ordered by value per unit of work. Each step is independently shippable.

Ordering rule: **companies gained per unit of new code and new risk.** That is
what demotes the blanket Greenhouse sweep from "largest single jump" (the first
draft's guess) to "do not run" (the measurement).

| # | Step | Effort | Expected gain | Status |
|---|---|---|---|---|
| 1 | Make the repo public; re-measure `JOB_ALERT_BROWSER_WORKERS` on the 4-vCPU runner before raising it | S | unlimited minutes, browser headroom | repo public; workers **deliberately still 2** |
| 2 | Make `JOB_ALERT_MAX_NEW_JOBS` proportional to corpus size | S | must land before the corpus grows | **done** 2026-08-19 |
| 3 | Gate and import the **224 new Comeet uids** | S | ~+90 companies, zero new code | **done** — 94 imported, 536 on-family |
| 4 | Build the **Workable adapter** | M | +38 companies | **done** — 16 passed the gates, 49 postings |
| 5 | Add `_onboarding/probed.csv` - persist negative results | S | makes every later sweep incremental | **done** |
| 6 | Build the **JSON-LD / `schema.org` JobPosting** generic fetcher | M | the only route to unbounded growth | **deferred on measurement** - 3% of self-hosted pages carry it |
| 7 | Re-source the company universe | M | replaces the exhausted seed | **now the only unblocker**; SNF returns 403 to scripted clients |
| 8 | Revisit the bare-`Remote` relevance rule | S | prerequisite for any global-platform expansion | **now evidenced** - see below |
| 9 | Tiered cadence (hot/warm/cold) + conditional requests | M | keeps run time flat as the corpus grows | not yet needed - 366 companies fetch in 75s |
| 10 | Mine Israeli aggregators for non-tech employers; adapters for Niloosoft etc. | L | the true long tail | |
| - | ~~Blanket A1 sweep across global ATS hosts~~ | - | **rejected on measurement** | 4,295 Greenhouse tenants yield ~43 Israeli boards, 82 already profiled |

### What steps 2-4 actually returned, against what was projected

| | Projected | Actual |
|---|---|---|
| Comeet companies | ~90 | **94** |
| Workable companies | 38 | **16** (22 rejected by the gates) |
| Corpus | 256 | **366** |
| Full-run wall clock | ~4 min *(est.)* | **75s**, 366/366, 0 failures |

The Workable number is the instructive one: the aggregator's 38 companies were
real, but 9 serve an empty account board, 8 have no on-family Israeli role, and
5 do not answer at all. **A discovery count is not an import count**, and the
gap is what the gates are for. The 141 postings the feed advertised became 49
once the project's own relevance and role rules were applied instead of the
vendor's location filter.

### 2026-08-20: two routes measured and closed

Both were run against the 1,127 unprofiled seed rows that carry a careers URL -
the last input the existing seed had left.

**The careers-page fingerprint route is exhausted, now with evidence.**
`sweep_careers_fingerprint.py` asked all 1,127 companies where their board is,
then scored every hit with the **real fetcher** (a temp profile, resolved
against its platform file, validated and fetched exactly as a committed one
would be - so these are the numbers the bot would see, not an estimate).

| | |
|---|---:|
| Companies probed | 1,127 |
| Fingerprinted to a known ATS | 54 (4.8%) |
| ...with an Israel-relevant on-family posting | **3** |
| Importable after `DELIBERATE_DROPS` | **2** — Johnson & Johnson, Red Hat |

The hits are real; they are just multinationals. Abbott, Accenture, AT&T all
fingerprint cleanly to Workday and return **zero** Israel-relevant target-family
postings. 26 of the 54 hits were Workday.

**A projection made from a sample was wrong, and it is worth saying why.** A
200-company sample suggested 12 Workday hits and therefore ~68 importable
companies. It was **size-weighted** toward `l`/`xl` - deliberately, per the yield
curve - and that is exactly where the multinationals with empty Israeli Workday
boards live. The fingerprint *rate* held (7.5% weighted vs 4.8% unweighted); what
did not survive was the assumption that a fingerprint implies an Israeli board.
**Weighting a sample toward yield also weights it toward a particular kind of
company, and that changes what the hits mean, not just how many there are.**

**JSON-LD is not the next fetcher to build.** `measure_jsonld.py` checked each
company's careers page and up to three job detail pages linked from it (Google
requires the markup on the detail page, so a listing-only check under-reports):

| Of 200 sampled | |
|---|---:|
| Already on a known ATS | 15 |
| Careers page unreachable | 52 |
| **Self-hosted with `JobPosting` JSON-LD** | **6 (3%)** |
| Self-hosted with no structured data at all | 127 |

**3%**, and 5 of the 6 only on a detail page. Projected across the whole pool
that is ~34 companies, each still needing its own listing-discovery and
pagination work. Step 6 is therefore **deferred, not cancelled** - it becomes
worthwhile when there is a larger pool of self-hosted Israeli companies to point
it at, which is step 7.

**Step 7 is now the only thing that unblocks growth, and its first source is
closed.** `finder.startupnationcentral.org` returns **HTTP 403 to any scripted
client** - search page, `_next` data routes and all. That is a deliberate access
control, and working around it is out of scope on principle. Startup Nation
Finder needs a legitimate access or partnership route, or the universe has to
come from somewhere else (the Israeli aggregators in section 3, or a paid
source-code index per A3).

**What this leaves.** Both directions out of the current seed are now measured
and closed: ATS fingerprinting yields 2, JSON-LD yields ~34 for a new fetcher.
The corpus grows from **new company sources**, not from better extraction of the
one we have. That was section 3's claim; it is now the measured conclusion
rather than an assertion.

### Step 8 is no longer a hypothesis

The bare-`Remote` rule was flagged here as a policy question. Both imports then
produced the evidence: whole *companies* qualify on it with no Israeli role at
all - CapsLock, MRIoA, BDR Solutions, OuterBox, ROI Agency, Medvidi, every one
a US employer with `location.country == "US"` and a label reading `Remote`.

A **company-level** gate now rejects those at admission, and that was the right
place for it: `relevance.py` is unchanged, so a company with a genuine Israeli
presence still gets the fail-open per-posting rule in full. But the underlying
asymmetry is still there - `is_israel_country_code` is additive by design, so a
foreign country code identifies nobody and rejects nobody. That is load-bearing
and was deliberately not touched. It is the thing to revisit before step 7.
