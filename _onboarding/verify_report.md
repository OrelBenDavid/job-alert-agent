# Phase 1 verification report

Run on **2026-08-13** against all 152 rows of `bot_shortlist.csv`, using the corrected
`discover_ats.py` in this directory. Every number below came from a live API call made
in this session. Full machine-readable output: `verify_raw.csv`.

## Headline

| | |
|---|---|
| Rows in shortlist | 152 |
| **Live** | **142** |
| Dead / not reachable | 10 |
| Total open positions across all live boards | 3,938 |
| **Total Israel-relevant** (project's own `relevance.is_relevant_location`) | **1,294** |

**Comeet works.** 104 of its 111 rows are live. The list does not collapse from 152 to
41, and Phase 2 onward can proceed as planned. This was the stated go/no-go risk, so
the detail is in its own section below.

## What the shortlist got wrong

**The `api_endpoint` column is wrong for all 111 Comeet rows and should not be used.**
It was built on the assumption `token == uid`. That assumption is false, and it fails
*closed*: every Comeet row returns HTTP 400 and would have been written off as dead. See
the Comeet section. The `id` column, by contrast, held up everywhere it could be checked —
including the `slug/uid` pairing, which turned out to be exactly what is needed.

`rnd_jobs` / `total_jobs` are a snapshot from 2026-08-12 and are consistently *lower*
than the live board size, because they counted R&D-ish roles in a feed while the API
returns the whole board worldwide. They were not used for anything here.

## Counting note

Israel-relevant counts use the project's own `relevance.is_relevant_location`, imported
from `src/`, not the coarse regex the handed-over script carried. The script's own
comment said that regex "is NOT the project's relevance filter and must not be reused as
one", so it was replaced rather than restated. This changes numbers in both directions:
it keeps qualified remote ("Remote - EMEA") and drops foreign-anchored remote
("Remote - US").

---

## Per-platform findings

### 1. Working endpoint URL templates (all verified live)

| Platform | Endpoint | Verified against |
|---|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | `datadog` (437), `forter` (40) |
| Lever | `https://api.lever.co/v0/postings/{slug}?mode=json` — EU: `api.eu.lever.co` | `mobileye` (137, EU), `walkme` (49, US) |
| Comeet | `https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}` | `vastdata/43.001` (238), `SolarEdge/71.00A` (110) |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{slug}` | `zafran-security` (26), `tavily` (18) |

### 2. Pagination — tested against the largest board on each platform

**No platform in this set paginates.** All four return the entire board in one call.
Each was tested against the biggest live board on that platform, not a small one:

- **Greenhouse / `datadog`, 437 jobs.** `len(jobs) == meta.total == 437`, so the
  response self-reports completeness. `&page=2` returns the same 437 — the parameter is
  ignored. 437 is well past any plausible 50- or 100-per-page cap.
- **Lever / `mobileye`, 137 jobs.** Default call returns all 137. **Lever does honour
  `limit` and `skip`** (`&limit=10` → 10; `&skip=100` → 37), so pagination *exists* —
  the default simply has no cap below 137. This is the one caveat on the page: a board
  larger than Lever's (undiscovered) default ceiling could truncate silently. 137 is the
  largest Lever board in this set, so it is unverified above that number, and it is
  written down here as unverified rather than assumed safe. The health gate's relative
  collapse check is what would catch it.
- **Comeet / `vastdata`, 238 jobs.** 238 items, 238 unique uids. `&page=2`, `&limit=10`,
  `&offset=100` and `&skip=100` *all* return the identical 238 — every pagination
  parameter is ignored.
- **Ashby / `zafran-security`, 26 jobs.** Flat `jobs` array, no cursor or total field.
  Both Ashby boards are small, so "does not paginate" is verified only at this size.

### 3. JSON paths

| | Greenhouse | Lever | Comeet | Ashby |
|---|---|---|---|---|
| container | `jobs[]` | *(top-level array)* | *(top-level array)* | `jobs[]` |
| stable id | `id` | `id` | `uid` | `id` |
| title | `title` | `text` | `name` | `title` |
| location | `location.name` **+ `offices[].name`** | `categories.location` | `location.name` | `location` **+ `secondaryLocations[]`** |
| absolute URL | `absolute_url` | `hostedUrl` | `url_comeet_hosted_page` | `jobUrl` |

Two of these need both location fields, not one, and getting it wrong under-reports
Israel jobs on exactly the boards most likely to have them:

- **Greenhouse** reports `location.name` as `"Multiple Locations"` routinely; `offices[]`
  is the only way to see whether Israel is one of them. The project's existing fetcher
  already does this.
- **Ashby** carries extra locations in `secondaryLocations[]`.

### 4. EU hosts

- **Lever:** confirmed a real split. Of the 8 live Lever rows, **7 are on `api.lever.co`
  and 1 (`mobileye`) is on `api.eu.lever.co`.** There is nothing in the id to tell them
  apart. Runtime detection is a fallback: call the US host, and on 404 (`{"ok":false,
  "error":"Document not found"}`) retry the EU host. `verify` now does this and records
  which host answered, in the `host` column of `verify_raw.csv`.
- **Greenhouse:** no EU host split found. All 28 live boards answer on
  `boards-api.greenhouse.io`. Greenhouse does not partition its public board API by
  region the way Lever does.

---

## Comeet in detail — the flagged risk

The endpoint template in the CSV was `?token={uid}` — the same value in both places.
Live, that returns:

```
HTTP 400 {"status":400,"message":"Account uid or token are not valid"}
```

for every one of the 111 rows.

What made this recoverable is that **Comeet distinguishes its two error messages**:
`"invalid company id"` (returned when the *uid* is wrong) versus `"Account uid or token
are not valid"`. Every row returned the second one — which proves the uid was right and
only the token was wrong.

The token is a separate, per-company, **public** value: it ships in a JSON blob embedded
in the company's Comeet-hosted board page, next to its uid —

```
"company_uid": "71.00A", "token": "17A5E876...", "slug": "SolarEdge"
```

It is genuinely per-company: SolarEdge's token against VAST Data's uid returns 400. So
resolving it costs one extra page fetch per company, once. `resolve_comeet_token()`
does this and then **confirms each candidate with a real API call before returning it**,
so a token it hands back is verified by construction rather than pattern-matched.

The `slug/uid` pairing in the CSV's `id` column is exactly what the board-page URL needs
(`https://www.comeet.co/jobs/{slug}/{uid}`), so no extra data was required.

**This does not need per-company investigation.** It is one deterministic, automated
resolution step that ran unattended across all 111 rows.

### Cost note for Phase 2

The board page is ~750 KB, versus a few KB for the API call. Resolving a token is by far
the most expensive request in the set. It should happen **once, at import time in
Phase 3**, with the token baked into the company record — not on every run. Doing it per
run would add 111 × 750 KB to every scheduled execution for data that changes almost
never.

---

## The 10 dead rows

Skipped in Phase 3, per the plan. None of these is a bug in the verification.

| Tier | Company | Platform | id | Evidence |
|:--:|---|---|---|---|
| A | HiBob | comeet | `hibob/12.00A` | board page = generic 404 shell |
| A | Viz.ai | greenhouse | `vizai` | `404 {"error":"Job not found"}` |
| B | Digital Turbine | lever | `digitalturbine` | 404 on **both** US and EU hosts |
| B | MASSIVit 3D Printing | comeet | `massivit/39.007` | board page = generic 404 shell |
| C | Cyberbit | comeet | `Cyberbit/C3.00E` | board page = generic 404 shell |
| C | CyberProof | comeet | `cyberproof/75.00F` | board page = generic 404 shell |
| C | Deep Instinct | comeet | `deepinstinct/72.00A` | board page = generic 404 shell |
| C | Insightec | greenhouse | `insightec` | `404 {"error":"Job not found"}` |
| C | NeoGames | comeet | `neogames/16.00C` | board page = generic 404 shell |
| C | REE Automotive | comeet | `ree/D3.00B` | board page = generic 404 shell |

All 7 dead Comeet accounts return a byte-for-byte identical 82,866-byte page — Comeet's
generic "no such board" shell, not a real board. That is a positive identification of a
closed/migrated account rather than an inference from a missing token.

These are dead *identifiers*, which is not the same as "not hiring". Several (HiBob,
Deep Instinct, REE) are real companies that have most likely moved ATS. Re-resolving
them is a Phase 6-ish task, not a blocker.

---

## Things worth flagging

**1. BioCatch is on two live boards at once.** It is the one duplicated company name in
the shortlist, and it is not a data error — both are live:

| id | platform | board | Israel-relevant |
|---|---|---|---|
| `biocatch/03.00E` | comeet | 22 | 9 |
| `biocatch` | lever | 13 | 5 |

The project convention already covers this ("merging multiple sources for one company:
merge into a dict keyed by `id`"). It needs an explicit decision in Phase 3 rather than
one row silently overwriting the other — which is exactly the collision case the plan
asked to handle explicitly.

**2. 14 live companies currently have 0 Israel-relevant jobs** (6 tier A, 2 tier B,
6 tier C). Their boards are live and non-empty — they just have nothing in Israel right
now. These need `zero_is_plausible: true` at import, or they will each fire a false
maintenance alert on their first run. This is the same call already made for `wiz`.

**3. Tier C was the weakest input and mostly held up.** 21 of 27 live, and 15 of those
have Israel-relevant openings — so "large R&D org, hiring unverified" converted better
than the tier name suggests. 6 of the 10 dead rows are tier C, which is consistent with
it being the least-sourced tier.

**4. Lever's `limit`/`skip` support** is the one place where "does not paginate" is a
statement about observed defaults rather than about the API's capabilities. Recorded
above rather than smoothed over.

---

## Full results — all 152 rows

Sorted live-first, then by platform, then by board size.

| # | Tier | Company | Platform | id | Status | Board | Israel-relevant |
|---:|:--:|---|---|---|---|---:|---:|
| 1 | A | Zafran Security | ashby | `zafran-security` | live | 26 | 9 |
| 2 | C | Tavily | ashby | `tavily` | live | 18 | 9 |
| 3 | A | VAST Data | comeet | `vastdata/43.001` | live | 238 | 38 |
| 4 | A | SolarEdge Technologies | comeet | `SolarEdge/71.00A` | live | 110 | 36 |
| 5 | A | Cellebrite | comeet | `Cellebrite/C3.00F` | live | 64 | 18 |
| 6 | A | Island | comeet | `island/09.00A` | live | 57 | 17 |
| 7 | C | Team8 | comeet | `team8/61.003` | live | 53 | 40 |
| 8 | A | Global-e | comeet | `global-e/62.002` | live | 52 | 18 |
| 9 | A | Coralogix | comeet | `coralogix/06.004` | live | 49 | 13 |
| 10 | A | Exodigo | comeet | `exodigo/89.005` | live | 49 | 11 |
| 11 | B | Cognyte | comeet | `cognyte/F2.009` | live | 49 | 12 |
| 12 | A | Ceragon Networks | comeet | `Ceragon/D3.003` | live | 48 | 13 |
| 13 | A | Netafim | comeet | `netafim/B7.002` | live | 48 | 29 |
| 14 | A | Fetcherr | comeet | `fetcherr/68.006` | live | 40 | 16 |
| 15 | A | Port | comeet | `port/59.004` | live | 39 | 10 |
| 16 | A | CEVA | comeet | `ceva/76.005` | live | 37 | 18 |
| 17 | A | Retym | comeet | `retym/C6.003` | live | 35 | 14 |
| 18 | A | NextSilicon | comeet | `nextsilicon/18.007` | live | 35 | 13 |
| 19 | A | Checkmarx | comeet | `checkmarx/C0.008` | live | 34 | 4 |
| 20 | C | Gilat Satellite Networks | comeet | `gilat/39.005` | live | 34 | 2 |
| 21 | A | ActiveFence | comeet | `activefence/D5.005` | live | 33 | 18 |
| 22 | A | Silverfort | comeet | `silverfort/54.007` | live | 30 | 11 |
| 23 | A | DriveNets | comeet | `drivenets/72.006` | live | 29 | 24 |
| 24 | A | Rapyd Financial Network | comeet | `rapyd/73.00E` | live | 29 | 9 |
| 25 | B | Viber | comeet | `viber/04.002` | live | 29 | 8 |
| 26 | C | eToro | comeet | `etoro/41.009` | live | 26 | 2 |
| 27 | A | Guardio | comeet | `guardio/57.000` | live | 25 | 25 |
| 28 | A | Skai | comeet | `skai/22.00A` | live | 25 | 4 |
| 29 | A | DealHub | comeet | `dealhub/86.005` | live | 24 | 15 |
| 30 | B | XTEND | comeet | `xtend/85.00A` | live | 23 | 11 |
| 31 | A | BioCatch | comeet | `biocatch/03.00E` | live | 22 | 9 |
| 32 | A | Wiliot | comeet | `wiliot/F6.003` | live | 21 | 9 |
| 33 | B | Atera Networks | comeet | `atera/63.00B` | live | 21 | 14 |
| 34 | A | Riverside.fm | comeet | `riverside-fm/66.009` | live | 20 | 13 |
| 35 | A | Claroty | comeet | `Claroty/F2.004` | live | 20 | 8 |
| 36 | A | Aitech | comeet | `aitechsystems/88.004` | live | 19 | 9 |
| 37 | A | BIRD Aerosystems | comeet | `birdaero/97.006` | live | 17 | 0 |
| 38 | A | Cross River Technologies | comeet | `crossriver/C7.00F` | live | 16 | 16 |
| 39 | B | SuperPlay | comeet | `superplay/28.003` | live | 16 | 12 |
| 40 | B | Cynet | comeet | `cynet/33.00D` | live | 16 | 4 |
| 41 | A | ACS Motion Control | comeet | `ACS/14.000` | live | 15 | 11 |
| 42 | A | Plus500 | comeet | `plus500/A1.00F` | live | 15 | 15 |
| 43 | A | ThetaRay | comeet | `thetaray/72.00F` | live | 15 | 4 |
| 44 | A | ONE ZERO Digital Bank | comeet | `onezerobank/36.00A` | live | 14 | 14 |
| 45 | A | Anchor | comeet | `anchorfintech/87.00D` | live | 14 | 8 |
| 46 | A | Kaltura | comeet | `kaltura/E2.00D` | live | 14 | 10 |
| 47 | A | Personetics Technologies | comeet | `personetics/83.00A` | live | 14 | 9 |
| 48 | B | Windward | comeet | `windward/31.002` | live | 14 | 3 |
| 49 | A | Arpeely | comeet | `arpeely/57.001` | live | 13 | 13 |
| 50 | A | Gett | comeet | `gett/A0.002` | live | 13 | 13 |
| 51 | B | Lumenis | comeet | `lumenis/A1.00C` | live | 13 | 6 |
| 52 | B | Pentera | comeet | `pentera/C5.00D` | live | 13 | 7 |
| 53 | C | Guesty | comeet | `guesty/10.000` | live | 13 | 2 |
| 54 | A | FullPath | comeet | `fullpath/54.002` | live | 12 | 12 |
| 55 | A | Innoviz Technologies | comeet | `innoviz/52.004` | live | 11 | 11 |
| 56 | A | Cardo Systems | comeet | `cardosystems/F2.003` | live | 11 | 6 |
| 57 | A | Landa Digital Printing | comeet | `landacorp/A4.000` | live | 11 | 4 |
| 58 | B | 365Scores | comeet | `365scores/B3.006` | live | 11 | 10 |
| 59 | A | Zero Networks | comeet | `zeronetworks/39.00F` | live | 10 | 5 |
| 60 | A | Enercon | comeet | `enercon/A4.00D` | live | 9 | 9 |
| 61 | B | Chargeflow | comeet | `chargeflow/29.001` | live | 9 | 6 |
| 62 | B | Daisy | comeet | `daisy/67.002` | live | 9 | 2 |
| 63 | B | Percepto | comeet | `percepto/44.000` | live | 9 | 4 |
| 64 | B | SodaStream | comeet | `sodastream/40.008` | live | 9 | 2 |
| 65 | A | Vectorious Medical Technologies | comeet | `vectoriousmedtech/68.003` | live | 8 | 6 |
| 66 | A | Eitan Medical | comeet | `eitanmedical/C6.00F` | live | 8 | 6 |
| 67 | A | Prisma Photonics | comeet | `prismaphotonics/18.00C` | live | 8 | 7 |
| 68 | B | Legit Security | comeet | `legitsecurity.com/37.004` | live | 8 | 4 |
| 69 | B | Clinch | comeet | `clinch/42.007` | live | 8 | 3 |
| 70 | C | LiveU | comeet | `liveu/90.00C` | live | 8 | 3 |
| 71 | A | mPrest | comeet | `mprest/38.005` | live | 7 | 7 |
| 72 | A | Aqua Security | comeet | `aquasec/91.001` | live | 7 | 5 |
| 73 | B | Surecomp | comeet | `Surecomp/24.00E` | live | 7 | 2 |
| 74 | B | Altair | comeet | `altair-semi/88.003` | live | 6 | 4 |
| 75 | B | Tikal | comeet | `tikalk/68.00C` | live | 6 | 5 |
| 76 | B | Agora | comeet | `agora/08.007` | live | 6 | 4 |
| 77 | B | Driivz | comeet | `driivz/C4.00B` | live | 6 | 3 |
| 78 | B | Waterfall Security Solutions | comeet | `waterfall-security/C7.009` | live | 6 | 5 |
| 79 | A | Final | comeet | `final/C0.009` | live | 5 | 0 |
| 80 | A | Gloat | comeet | `gloat/E5.000` | live | 5 | 5 |
| 81 | A | KMS Lighthouse | comeet | `kmslh/97.008` | live | 5 | 2 |
| 82 | A | WeTrip | comeet | `weski/F8.00C` | live | 5 | 5 |
| 83 | B | Cycode | comeet | `cycode/A8.00D` | live | 5 | 3 |
| 84 | B | ScyllaDB | comeet | `scylladb/E4.006` | live | 5 | 1 |
| 85 | C | Panaya | comeet | `panaya/61.00C` | live | 5 | 1 |
| 86 | A | Imagen | comeet | `imagen-ai/78.00F` | live | 4 | 0 |
| 87 | A | Toka | comeet | `toka/46.00D` | live | 4 | 3 |
| 88 | B | Lusha | comeet | `lusha/73.00B` | live | 4 | 4 |
| 89 | A | Shopic | comeet | `shopic/E6.002` | live | 3 | 3 |
| 90 | B | Optibus | comeet | `optibus/D1.00C` | live | 3 | 2 |
| 91 | B | LSports | comeet | `lsports/26.002` | live | 3 | 1 |
| 92 | B | MDClone | comeet | `mdclone/66.004` | live | 3 | 2 |
| 93 | B | WSC Sports Technologies | comeet | `wsc-sports/93.007` | live | 3 | 1 |
| 94 | C | Trigo | comeet | `trigo/A6.005` | live | 3 | 2 |
| 95 | B | 8fig | comeet | `8fig/08.004` | live | 2 | 2 |
| 96 | B | Coro | comeet | `coro/08.00A` | live | 2 | 2 |
| 97 | B | Rise | comeet | `risecodes/A9.000` | live | 2 | 2 |
| 98 | B | GK8 | comeet | `gk8/E8.000` | live | 1 | 0 |
| 99 | C | Bookaway | comeet | `Bookaway/64.006` | live | 1 | 1 |
| 100 | C | Browzwear | comeet | `browzwear/03.000` | live | 1 | 0 |
| 101 | A | Aidoc | comeet | `Aidoc/B4.007` | live | 0 | 0 |
| 102 | A | Moon Active | comeet | `moonactive/A2.00C` | live | 0 | 0 |
| 103 | B | Playstudios | comeet | `playstudios/E2.00B` | live | 0 | 0 |
| 104 | C | AI21 Labs | comeet | `ai21/E6.001` | live | 0 | 0 |
| 105 | C | Fabric | comeet | `fabric/52.009` | live | 0 | 0 |
| 106 | C | Novidea Software | comeet | `novidea/E5.00A` | live | 0 | 0 |
| 107 | B | Datadog | greenhouse | `datadog` | live | 437 | 16 |
| 108 | A | Navan | greenhouse | `TripActions` | live | 209 | 8 |
| 109 | C | SentinelOne | greenhouse | `sentinellabs` | live | 205 | 17 |
| 110 | C | Gong | greenhouse | `gongio` | live | 100 | 16 |
| 111 | A | Taboola | greenhouse | `taboola` | live | 98 | 29 |
| 112 | A | Fireblocks | greenhouse | `fireblocks` | live | 71 | 12 |
| 113 | A | SimilarWeb | greenhouse | `similarweb` | live | 65 | 23 |
| 114 | C | Verifone | greenhouse | `verifone` | live | 42 | 1 |
| 115 | A | Forter | greenhouse | `forter` | live | 40 | 15 |
| 116 | A | Tipalti | greenhouse | `tipaltisolutions` | live | 37 | 5 |
| 117 | C | Aiven | greenhouse | `aiven36` | live | 37 | 1 |
| 118 | A | Axonius | greenhouse | `axonius` | live | 36 | 10 |
| 119 | A | JFrog | greenhouse | `jfrog` | live | 34 | 11 |
| 120 | A | Armis | greenhouse | `armissecurity` | live | 26 | 11 |
| 121 | A | Playtika | greenhouse | `playtikaltd` | live | 26 | 19 |
| 122 | B | Riskified | greenhouse | `riskified` | live | 23 | 5 |
| 123 | A | Transmit Security | greenhouse | `transmitsecurity` | live | 15 | 11 |
| 124 | B | Pagaya | greenhouse | `pagayais` | live | 15 | 15 |
| 125 | B | Placer.ai | greenhouse | `placerlabs` | live | 12 | 2 |
| 126 | A | Orca Security | greenhouse | `orcasecurity` | live | 11 | 3 |
| 127 | B | Cymulate | greenhouse | `cymulate` | live | 11 | 5 |
| 128 | C | Cybereason | greenhouse | `cybereason` | live | 8 | 0 |
| 129 | A | Lightricks | greenhouse | `lightricks` | live | 6 | 5 |
| 130 | C | Duda | greenhouse | `duda` | live | 6 | 0 |
| 131 | C | Optimove | greenhouse | `optimove` | live | 6 | 2 |
| 132 | B | Sweet Security | greenhouse | `sweetsecurity` | live | 4 | 4 |
| 133 | A | Wiz | greenhouse | `wizprivate` | live | 2 | 0 |
| 134 | B | Capitolis | greenhouse | `capitolis` | live | 2 | 2 |
| 135 | A | Mobileye | lever_eu | `mobileye` | live | 137 | 117 |
| 136 | A | WalkMe | lever | `walkme` | live | 49 | 17 |
| 137 | A | D-Fend Solutions | lever | `d-fendsolutions` | live | 31 | 25 |
| 138 | A | Parallel Wireless | lever | `parallelwireless` | live | 31 | 8 |
| 139 | A | Cloudinary | lever | `cloudinary` | live | 29 | 11 |
| 140 | A | BioCatch | lever | `biocatch` | live | 13 | 5 |
| 141 | C | Sauce | lever | `Sauce` | live | 13 | 1 |
| 142 | A | CYE | lever | `CYE` | live | 11 | 9 |
| 143 | A | HiBob | comeet | `hibob/12.00A` | **DEAD** | — | — |
| 144 | B | MASSIVit 3D Printing Technologies | comeet | `massivit/39.007` | **DEAD** | — | — |
| 145 | C | Cyberbit | comeet | `Cyberbit/C3.00E` | **DEAD** | — | — |
| 146 | C | CyberProof | comeet | `cyberproof/75.00F` | **DEAD** | — | — |
| 147 | C | Deep Instinct | comeet | `deepinstinct/72.00A` | **DEAD** | — | — |
| 148 | C | NeoGames | comeet | `neogames/16.00C` | **DEAD** | — | — |
| 149 | C | REE Automotive | comeet | `ree/D3.00B` | **DEAD** | — | — |
| 150 | A | Viz.ai | greenhouse | `vizai` | **DEAD** | — | — |
| 151 | C | Insightec | greenhouse | `insightec` | **DEAD** | — | — |
| 152 | B | Digital Turbine | lever | `digitalturbine` | **DEAD** | — | — |
