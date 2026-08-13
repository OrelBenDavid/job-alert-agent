# Experience-filter coverage audit

Run 2026-08-13, after Comeet description access was implemented. Every one of the
145 companies was fetched live and up to 3 real postings per company were pushed
through the **actual** detail layer and the **actual** parser - not inspected,
executed. 360 postings in total.

## Result by platform

| Platform | Method | Companies | Postings | Got a description | Determined years |
|---|---|---:|---:|---:|---:|
| ashby | `inline` | 3 | 8 | 8 | 3 |
| comeet | `embedded_json` | 105 | 259 | 259 | 160 |
| greenhouse | `inline` | 28 | 68 | 68 | 23 |
| hibob | `inline` | 1 | 3 | 3 | 3 |
| lever | `inline` | 7 | 19 | 19 | 13 |
| standalone | `NONE` | 1 | 3 | 0 | 0 |
| **TOTAL** | | **145** | **360** | **357** | **202** |

**357 of 360 sampled postings (99%) now reach the parser with real
description text.** Before this change that number was 99 - only the 39 non-Comeet
companies - because Comeet was declared `detail_fetch: none` on a claim that turned
out to be false.

## The one remaining gap: wix

`wix` is the only company where the detail layer gets nothing (0 of 3 postings).
Checked live: a Wix posting page is a pure JS shell - **no** description, no
JSON-LD, no `JobPosting` markup, not a single occurrence of "years" in 483 KB of
served HTML. So `html` and `embedded_json` are both ruled out, and reaching it
would need a `playwright` detail fetch: one Chromium page load per new posting.
That is not a new dependency for this company (its listing already needs a
browser), but it does need a browser session to establish a content selector, and
it is 1 company of 145. Left undone deliberately, and its jobs keep arriving
flagged.

## Determination rate is not a defect

"Got a description" and "determined years" are different things, and the gap
between them is mostly the parser being deliberately conservative rather than
failing.

`read_experience` takes the minimum across blocks it can classify as **mandatory**.
A bullet qualifies by containing a word like "required"/"must", or by sitting
under a requirements-shaped heading. A bare "5 years of experience" in an unheaded
paragraph is left **unknown** on purpose - counting it would re-admit the
"5+ years preferred" false rejection the per-bullet design exists to prevent, and a
false rejection silently withholds a job the user should have seen.

That is why the rates differ so much by platform, and the difference is about how
recruiters write, not about the fetcher:

- **Lever 68%, Comeet 62%** - both emit named sections (`lists`, `custom_fields.details`),
  so "Requirements" becomes a real heading and promotes the bullets under it.
- **Greenhouse 34%** - `content` is one flat HTML blob. Spot-checked live: Taboola
  parses fine, while Gong heads its sections "You'll Own:" / "You are:" and Forter
  uses no headings at all, so their numbers stay unclassified and those jobs pass
  flagged.

Widening the heading list (e.g. adding "you are") would raise the Greenhouse rate
and would also move the failure in the **unrecoverable** direction - more
rejections, some of them wrong. Not changed here; it is a product decision about
how much risk of a missed job is acceptable, not a bug fix.

## Companies with no open Israel-relevant jobs right now (14)

Nothing to sample, so they are absent from the counts above rather than failing.
All carry `zero_is_plausible: true`.

```
ai21_labs, aidoc, bird_aerosystems, browzwear, cybereason, duda, fabric, final, gk8, imagen, moon_active, novidea_software, playstudios, wiz
```
