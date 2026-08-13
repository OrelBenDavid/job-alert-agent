# Re-resolving the 10 dead identifiers

Done 2026-08-13 with the `career-site-profiler` skill. Every company below was
investigated from its own careers page (Step 0 → Step 1a → Step 1b), and for the
JS-rendered ones, in a browser reading the actual network calls.

**Three were recovered and are now imported and seeded. Seven stay out**, each for a
stated reason.

## Correction to the Phase 1 verdict

The Phase 1 report claimed the 82,866-byte Comeet shell was "a positive identification of
a closed/migrated account." That was wrong. The board-page URL is `/jobs/{slug}/{uid}`,
and a **known-good uid with a wrong slug returns the identical shell** — so the test could
not tell a dead account from a stale slug.

REE Automotive is the proof: uid `D3.00B` was correct all along and its Comeet account is
live and rendering jobs today. The row was still correctly *excluded* (it would not have
fetched), but the reason given was stronger than the evidence supported.

A second, broader lesson: **the shortlist's `ats` column was wrong, not merely stale, for
two of the three recoveries.** Viz.ai and Insightec were both listed as Greenhouse and are
on Ashby and Comeet respectively. Re-resolving on the assumption that only the *identifier*
had changed would have found neither.

---

## Recovered (3)

### Viz.ai → Ashby, slug `Viz.ai`

`www.viz.ai/careers` is a landing page; its "View open positions" call-to-action is a
plain link to `www.viz.ai/jobs`, which embeds `jobs.ashbyhq.com/Viz.ai?embed=js`. This is
exactly the Step 0.5 case — a real, working branded page that links out to the actual
board with no HTTP redirect.

The slug is **`Viz.ai` verbatim**, capital V and a literal dot. No lowercase slug guess
would ever have found it, which is why the brute-force probe in `discover_ats.py` did not.

15 postings, 2 Israel-relevant. Ashby was already implemented, so this cost no code.

### Insightec → Comeet, uid `4A.004`

`insightec.com/careers/` runs the Comeet WordPress plugin and its page source carries the
widget config verbatim: `comeet_uid` `4A.004` and its token. Confirmed live — 22 postings,
5 Israel-relevant (Haifa, Kiryat Ono, plus EMEA-remote).

The shortlist had this as Greenhouse `insightec`, which 404s.

### HiBob → its own product

HiBob has moved off Comeet entirely and now runs hiring on its own careers product:
`www.hibob.com/careers/` links every posting to
`hibob-fa0ad69d0cb34a.careers.hibob.com/jobs/<uuid>`, backed by a JSON endpoint at
`/api/job-ad`.

**Only the browser found this.** The page source still contains Comeet CSS class names
left over from the old integration, so the signature scan reported "Comeet" and found no
uid — a false positive that a source-only pass would have recorded as "still Comeet,
identifier dead."

62 postings, **17 Israel-relevant** — more than most companies in the corpus. This one
needed a new platform handler; see the notes in `profiles/_platforms/hibob.json` for the
two things about it worth knowing (the required `Referer` header, and `inline_prefix`).

---

## Not recovered (7)

### On a platform with no handler here (4)

| Company | Now on | Note |
|---|---|---|
| Digital Turbine | **Workday** `digitalturbine.wd501.myworkdayjobs.com/Digital_Turbine_External_Careers` | Its careers page *still calls* `api.lever.co/v0/postings/digitalturbine` from stale JS — the dead endpoint the shortlist recorded. The live board is Workday. |
| NeoGames | **Workday**, under Aristocrat | Acquired; `neogames.com` now redirects to `aristocratinteractive.com`, board `AristocratExternalCareersSite`. No longer an Israeli-HQ company in the sense the shortlist meant. |
| CyberProof | **RippleHire** (`usource.ripplehire.com`) | UST-group ATS, hash-routed SPA. |
| Cyberbit | **BambooHR** (`rangeforce.bamboohr.com`) | Merged with RangeForce; hiring runs under the RangeForce entity. |

Workday is the only one of these that appears twice, and it is in the skill's platform
table as UNVERIFIED. Adding it is a real piece of work — it is a `POST` with a JSON body,
unlike every platform here — and two companies is thin justification. Worth revisiting
only if a Workday sweep of the seed list turns up more.

### Self-hosted, no ATS (3)

| Company | Finding |
|---|---|
| Deep Instinct | Jobs are server-rendered directly into `deepinstinct.com/careers`. **5 roles, all Sales/CS, all US or Japan remote — zero Israeli openings.** A `fetch_type: html` profile is possible but currently buys nothing. |
| MASSIVit | Server-rendered WordPress page. **1 open role** ("Purchase Leader", Israel) — not R&D. The Comeet plugin's CSS is still on the page but no live widget. |
| REE Automotive | Comeet uid `D3.00B` is live and the WordPress plugin renders 2 roles (1 Israeli, Glil Yam) — but the **token published in the page config returns HTTP 400** against the public API, because the plugin fetches server-side with a different credential. So there is no usable API path; an `html` profile against `ree.auto/careers/` would work, for 1 Israeli role. |

All three are `html`-profile candidates rather than dead ends. None was written, because
each is a bespoke scrape — the fragile, high-maintenance category this project deliberately
keeps to a minimum — for a combined **2 Israeli roles**. Revisit if any of them grows.

---

## What was not verified

- The seven above were identified but **not** profiled: no selectors were recorded and no
  fetch was attempted against them, so nothing here claims they *would* work.
- HiBob's `/api/job-ad` was verified against one tenant only. Whether the same shape holds
  for other HiBob customers is unknown and untested.
- Cyberbit's board was identified from a link on its careers page; the BambooHR endpoint
  itself was never called.
