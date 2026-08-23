# -*- coding: utf-8 -*-
"""
Corpus health: are the companies we watch actually capable of delivering
anything, and is anything silently broken?

*** Why this exists, and what it is a detector for ***

EXPANSION_STRATEGY.md section 7 asks for this by name: "postings per company,
alerts per company per month, and the count of companies that have never
delivered a single alert. The Comeet location bug was found because four
companies had never delivered anything - that metric is a working detector and
should be a standing report, not an incidental discovery."

That is the whole argument. On 2026-08-19 `bird_aerosystems`, `final`,
`imagen` and `gk8` had never delivered a single alert in the project's
history, because the Comeet fetcher read a free-text label as if it were a
place. Nothing in the system could have caught it: their fetch succeeded,
their count was a steady and entirely plausible 0, so the health gate had
nothing to compare against, and `zero_is_plausible` was true because it was
true on the day they were imported. Every per-run check looks at a company
against ITSELF. This one looks at the corpus, where "delivers nothing, ever"
is visible and "0 again" is not.

At 145 companies that bug hid for months. At 368 it hides better, and the
report costs no requests - it reads only what is already on disk.

*** The proxy, stated plainly ***

`state/seen/<slug>.json` does NOT record whether an alert was ever sent. There
is no such field and this module does not pretend otherwise. What it has is
the set of postings ever seen, each with its stored title and `first_seen`, so
two proxies are computed and both are labelled as proxies:

  DELIVERABLE  - the posting's stored title, run back through roles.classify,
                 lands somewhere the role filter passes (`target`, or
                 `unknown`, which is sent flagged). A company with zero of
                 those cannot ever have sent anything and cannot ever send
                 anything, whatever its fetch does. This is the headline.

  CHURN        - a posting whose `first_seen` is later than the company's
                 earliest one. Seeding stamps every posting with a single
                 timestamp, so the earliest cohort is "what was already open
                 when we started" and anything after it is a posting this
                 project actually DETECTED. Weaker: a small board legitimately
                 goes weeks without a new req, so this is a watchlist ranked
                 by board size, never a verdict.

*** Read-only, by construction ***

This module imports `state.load_state` and `stats.load_stats` and nothing that
writes. It is a diagnostic: a tool that reports on state must never be able to
change the thing it is reporting on, or the first question about any finding
becomes "did the report cause it".
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import profiles as profiles_mod
import roles
import state as state_mod
import stats as stats_mod

# --------------------------------------------------------------------------
# Thresholds. Each is sized against something measured, not chosen for looks.
# --------------------------------------------------------------------------

# The cron in check.yml fires every 3 hours - 8 runs a day. A company whose
# last success is older than this has missed eight consecutive runs while the
# rest of the corpus succeeded, which no transient outage explains.
STALE_SUCCESS_HOURS = 24

# EXPANSION_STRATEGY.md 8.4: "Set health numbers from the live verified count
# and re-verify them on a schedule. A stale expected_min_jobs produces a false
# maintenance alert, which is how the 2026-08-19 pass started." 90 days is one
# quarter - long enough that re-verification is not busywork, short enough that
# a board's shape has not silently changed twice over.
STALE_VERIFIED_DAYS = 90

# Below this age "no posting detected since seeding" says nothing at all - and
# the number is measured, not guessed. Of the 128 companies seeded on
# 2026-08-13, exactly 63 (49%) had detected at least one new posting 6.9 days
# later. A 7-day window would therefore put half the corpus on the watchlist
# and discriminate nothing. 14 days is the first window where a company that
# has still moved nothing is saying something about itself rather than about
# the window; re-measure it once the corpus has that much history, because
# today it means the metric reports "not applicable yet" for almost everyone -
# which the report says out loud rather than printing a reassuring 0.
NO_CHURN_MIN_AGE_DAYS = 14

# A platform is flagged as underperforming when its structurally-silent rate is
# this multiple of the corpus rate. Two rather than something tighter because
# platforms genuinely differ - a global ATS carries more foreign-only boards
# than an Israeli vendor does - and the shape being hunted here is much louder
# than a factor of two. On 2026-08-19 Comeet's silent rate was several times
# the rest of the corpus's, because one field was being read wrongly on every
# single one of its boards.
PLATFORM_SILENCE_MULTIPLE = 2.0

# Below this a platform's rate is arithmetic noise: on a 3-company platform one
# legitimately empty board is 33%.
PLATFORM_MIN_COMPANIES = 5

# A board holding this many postings or fewer is treated as suspicious - not
# as small. See CompanyHealth.implausible_board for the case it was sized
# against ('wizprivate', 2 postings, standing in for a company with 124).
#
# 3 rather than something larger because the two error rates are not
# symmetrical. A false positive costs one look at a careers page; a false
# negative is a company delivering nothing for months while every number about
# it reads as healthy. The corpus this was set on has 166 of 368 companies at
# 1-4 Israel-relevant postings, but those are POST-filter counts - a raw board
# of 3 or fewer is a different and much rarer thing, and on 2026-08-23 it
# picked out 7 companies of 367, six of which were real findings.
BOARD_TOO_SMALL = 3


def _parse_dt(raw) -> datetime | None:
    """An ISO timestamp from state, or None. Never raises: a hand-edited or
    truncated value must degrade to "unknown" and leave every other company's
    line in the report intact."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_date(raw) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _hours_since(when: datetime | None, now: datetime) -> float | None:
    if when is None:
        return None
    return (now - when).total_seconds() / 3600.0


@dataclass
class CompanyHealth:
    """One company's answer to "could this ever deliver, and is it healthy"."""
    slug: str
    name: str
    platform: str
    enabled: bool
    seeded: bool
    unreadable: str | None = None

    # Classification of every stored title. non_job is counted separately
    # because filters.RoleFilter drops those outright rather than flagging
    # them - an evergreen "send us your CV" card is not a posting, so counting
    # it as deliverable would let a company look alive on nothing.
    tracked: int = 0
    target: int = 0
    unknown: int = 0
    blocked: int = 0
    non_job: int = 0

    seed_cohort: int = 0
    detected_since_seed: int = 0
    seeded_at: datetime | None = None

    last_success: datetime | None = None
    last_count: int = 0
    # How many postings the whole board held, before the Israel filter. None
    # for a company whose fetcher does not report it (html, playwright) and for
    # every state file written before 2026-08-23. See models.JobList.
    last_board_total: int | None = None
    consecutive_failures: int = 0
    expected_min_jobs: int = 0
    zero_is_plausible: bool = False
    verified_on: date | None = None
    has_detail_fetch: bool = False

    @property
    def deliverable(self) -> int:
        """Postings whose title the role filter passes today - `target` plus
        `unknown`, because the role filter fails open on `unknown` and sends it
        flagged. Using `target` alone would report a company whose entire board
        is `DFIR` / `CyOps Analyst` as silent when it delivers every one of
        them."""
        return self.target + self.unknown

    @property
    def structurally_silent(self) -> bool:
        return self.seeded and self.deliverable == 0

    @property
    def off_family_only(self) -> bool:
        """Tracks postings, but not one of them is on-family. Distinct from an
        empty board: the fetch demonstrably works, so this is a sourcing
        decision that went wrong (gate 4 in EXPANSION_STRATEGY.md 7), not a
        broken scraper."""
        return self.seeded and self.tracked > 0 and self.deliverable == 0

    @property
    def id_collisions(self) -> int:
        """Postings the fetch returned that state could not keep apart.

        `process_company` writes `last_count = len(fetched)` and `jobs` keyed
        by `Job.id` in the same breath, so on a healthy run the two must agree.
        They disagree only when two postings carried the SAME id, and the dict
        silently kept one - which is not a display problem, it is a permanent
        silent loss: the diff runs on `Job.id`, so every future posting that
        lands on that id reads as already-seen and can never be alerted.

        Only meaningful while the gate is open. Once `consecutive_failures` is
        non-zero the state is deliberately frozen - `last_count` describes the
        run that was rejected and `jobs` describes an older one - so comparing
        them there would manufacture a finding out of the gate doing its job.
        """
        if self.consecutive_failures or not self.seeded:
            return 0
        return max(0, self.last_count - self.tracked)

    @property
    def implausible_board(self) -> bool:
        """The board is too small to be this company's real careers board.

        *** The detector the corpus audit of 2026-08-23 was built out of ***

        Every other check here reads a post-filter number, and post-filter
        numbers cannot tell a real zero from a wrong endpoint - both are 0,
        both are stable, both look healthy. `wiz` sat in the repo for eleven
        days pointed at Greenhouse board_token 'wizprivate', which answers HTTP
        200 with two postings on it; the company hires on 'wizinc', which had
        124 postings and 22 Israel-relevant. A test asserted the mistake was a
        fact about the company.

        The raw board size is what separates them, and the threshold is
        deliberately crude. This is not "is this board small" - plenty of real
        Israeli boards carry three postings. It is "is this board so small that
        it cannot be a company's whole careers page", which at BOARD_TOO_SMALL
        is a claim about the endpoint rather than about hiring. It is a
        watchlist, not a verdict: a genuinely tiny startup will appear here and
        the answer is to look once and move on."""
        return (self.seeded and self.last_board_total is not None
                and self.last_board_total <= BOARD_TOO_SMALL)

    @property
    def board_yield(self) -> float | None:
        """Israel-relevant postings as a fraction of the whole board.

        None when the board size is unknown or empty. Low is not wrong - a
        global ATS with one Israeli office legitimately sits near zero - which
        is why this is reported as a distribution and never alerted on."""
        if not self.last_board_total:
            return None
        return self.last_count / self.last_board_total

    @property
    def below_floor(self) -> bool:
        """Sitting below its own declared floor right now.

        The health gate only fires when the PREVIOUS run was at or above the
        floor (`count < floor <= previous`), and it accepts the new count after
        three runs. So a company that settled below its floor is invisible to
        the gate forever after - either the floor is stale (Datadog, lowered
        from 4 to 0 on 2026-08-19) or the fetch is returning a truncated set.
        Only the report can see it."""
        return (self.seeded and self.expected_min_jobs > 0
                and self.last_count < self.expected_min_jobs)


@dataclass
class CorpusReport:
    generated_at: datetime
    companies: list[CompanyHealth] = field(default_factory=list)
    profile_errors: list[str] = field(default_factory=list)
    orphan_state: list[str] = field(default_factory=list)
    filter_stats: dict = field(default_factory=dict)

    # --- aggregates, all derived so nothing can drift out of sync ---

    @property
    def watched(self) -> list[CompanyHealth]:
        return [c for c in self.companies if c.enabled]

    @property
    def tracked_postings(self) -> int:
        return sum(c.tracked for c in self.watched)

    @property
    def deliverable_postings(self) -> int:
        return sum(c.deliverable for c in self.watched)

    @property
    def silent(self) -> list[CompanyHealth]:
        return [c for c in self.watched if c.structurally_silent]

    @property
    def unseeded(self) -> list[CompanyHealth]:
        return [c for c in self.watched if not c.seeded]

    def by_platform(self) -> dict[str, list[CompanyHealth]]:
        grouped: dict[str, list[CompanyHealth]] = {}
        for c in self.watched:
            grouped.setdefault(c.platform, []).append(c)
        return dict(sorted(grouped.items(),
                           key=lambda kv: (-len(kv[1]), kv[0])))

    def underperforming_platforms(self) -> list[tuple[str, float, float]]:
        """(platform, its silent rate, the corpus rate) for every platform
        materially worse than the corpus as a whole.

        This is the shape the Comeet bug made: not a failing company, but one
        platform's boards being quietly wrong together while every other
        platform stayed normal."""
        watched = self.watched
        if not watched:
            return []
        corpus_rate = len(self.silent) / len(watched)
        flagged = []
        for platform, members in self.by_platform().items():
            if len(members) < PLATFORM_MIN_COMPANIES:
                continue
            rate = sum(1 for c in members if c.structurally_silent) / len(members)
            if rate > 0 and rate >= corpus_rate * PLATFORM_SILENCE_MULTIPLE:
                flagged.append((platform, rate, corpus_rate))
        return sorted(flagged, key=lambda t: -t[1])

    def id_collisions(self) -> list[CompanyHealth]:
        return sorted((c for c in self.watched if c.id_collisions > 0),
                      key=lambda c: -c.id_collisions)

    def failing(self) -> list[CompanyHealth]:
        return sorted((c for c in self.watched if c.consecutive_failures > 0),
                      key=lambda c: -c.consecutive_failures)

    def below_floor(self) -> list[CompanyHealth]:
        return sorted((c for c in self.watched if c.below_floor),
                      key=lambda c: c.last_count - c.expected_min_jobs)

    def stale_success(self) -> list[CompanyHealth]:
        out = []
        for c in self.watched:
            if not c.seeded:
                continue
            hours = _hours_since(c.last_success, self.generated_at)
            if hours is None or hours > STALE_SUCCESS_HOURS:
                out.append(c)
        return out

    def stale_verification(self) -> list[CompanyHealth]:
        today = self.generated_at.date()
        out = []
        for c in self.watched:
            if c.verified_on is None:
                out.append(c)
            elif (today - c.verified_on).days > STALE_VERIFIED_DAYS:
                out.append(c)
        return out

    def churn_eligible(self) -> list[CompanyHealth]:
        """Companies old enough for the churn proxy to say anything."""
        out = []
        for c in self.watched:
            if not c.seeded or c.tracked == 0:
                continue
            hours = _hours_since(c.seeded_at, self.generated_at)
            if hours is not None and hours >= NO_CHURN_MIN_AGE_DAYS * 24:
                out.append(c)
        return out

    def no_churn(self) -> list[CompanyHealth]:
        """Companies old enough for the churn proxy to mean something that
        have not detected a single posting since they were seeded. Ranked by
        board size: a 40-posting board that never moves is a far stronger
        signal than a 2-posting one, which is ordinary."""
        return sorted((c for c in self.churn_eligible()
                       if not c.detected_since_seed),
                      key=lambda c: -c.tracked)

    def undeterminable(self) -> list[CompanyHealth]:
        """Companies with no way to reach a posting's description, so every
        posting they ever produce is `undetermined` in the experience filter
        and is delivered fail-open with a warning flag. Not a defect - it is
        correct fail-open behaviour - but it is the only per-company view of
        the filter funnel that the data supports, because filter_stats.json is
        corpus-wide and records no slug."""
        return [c for c in self.watched if c.seeded and not c.has_detail_fetch]


def _classify_titles(jobs: dict, health: CompanyHealth) -> None:
    """Runs every stored title back through the live classifier.

    The titles on disk are the ones the fetcher saw, so this measures the
    corpus against TODAY's roles.py - which is the point. When 14 Hebrew terms
    were added on 2026-08-19 the question "what does that do to the corpus" was
    answered by a throwaway script; this makes it a standing answer."""
    for entry in jobs.values():
        title = (entry or {}).get("title") or ""
        health.tracked += 1
        if roles.is_non_job(title):
            health.non_job += 1
            continue
        classification, _ = roles.classify(title)
        if classification == "target":
            health.target += 1
        elif classification == "blocked":
            health.blocked += 1
        else:
            health.unknown += 1


def _churn(jobs: dict, health: CompanyHealth) -> None:
    """Splits the tracked postings into the seed cohort and everything after.

    seed_company() stamps one `now` onto every posting it writes, so the
    earliest `first_seen` in the file identifies the whole seed batch to the
    microsecond. Anything later arrived through process_company's diff, which
    is the only other writer - so it is a posting this project detected rather
    than inherited. A posting with an unparseable timestamp counts as detected
    rather than as seed, erring toward "this company is alive" so the report
    cannot manufacture a silent company out of a formatting problem."""
    stamps = [_parse_dt((e or {}).get("first_seen")) for e in jobs.values()]
    known = [s for s in stamps if s is not None]
    if not known:
        health.detected_since_seed = len(stamps)
        return
    earliest = min(known)
    health.seeded_at = earliest
    health.seed_cohort = sum(1 for s in known if s == earliest)
    health.detected_since_seed = len(stamps) - health.seed_cohort


def _platform_of(profile) -> str:
    """What to group this company under.

    `platform` names the _platforms/ file it inherits from, which is the right
    key for every thin record. A standalone profile has none, so it falls back
    to api.platform and finally to fetch_type - which is how wix, the project's
    one playwright company, reports as `playwright` rather than as a blank."""
    raw = profile.raw
    return (raw.get("platform")
            or (raw.get("api") or {}).get("platform")
            or raw.get("fetch_type") or "unknown")


def collect(profiles_dir: Path | None = None,
            state_dir: Path | None = None,
            now: datetime | None = None) -> CorpusReport:
    """Builds the whole report. No network, no writes.

    Both directories are parameters so the tests can run the real code over a
    fixture corpus. `now` is a parameter for the same reason - every staleness
    threshold here is relative to it, and a test that depends on the wall clock
    is a test that starts failing on its own."""
    now = now or datetime.now(timezone.utc)
    profiles_dir = profiles_dir or profiles_mod.PROFILES_DIR
    state_dir = state_dir or state_mod.STATE_DIR

    loaded, errors = profiles_mod.load_all(profiles_dir)
    report = CorpusReport(generated_at=now, profile_errors=errors)

    for profile in loaded:
        health = CompanyHealth(
            slug=profile.slug, name=profile.name,
            platform=_platform_of(profile), enabled=profile.enabled,
            seeded=(state_dir / f"{profile.slug}.json").exists(),
            expected_min_jobs=profile.expected_min_jobs,
            zero_is_plausible=profile.zero_is_plausible,
            verified_on=_parse_date(profile.raw.get("verified_on")),
            has_detail_fetch=profile.detail_fetch is not None,
        )

        try:
            stored = state_mod.load_state(profile.slug, state_dir)
        except state_mod.StateUnreadable as e:
            # Surfaced, never swallowed. An unreadable state file means the
            # run cannot diff this company at all, and run.py skips it - so it
            # is silent in exactly the way this report exists to find, and it
            # must not be reported as a healthy zero.
            health.unreadable = str(e)
            report.companies.append(health)
            continue

        health.last_success = _parse_dt(stored.get("last_success"))
        health.last_count = int(stored.get("last_count") or 0)
        raw_board = stored.get("last_board_total")
        health.last_board_total = None if raw_board is None else int(raw_board)
        health.consecutive_failures = int(stored.get("consecutive_failures") or 0)
        jobs = stored.get("jobs") or {}
        _classify_titles(jobs, health)
        _churn(jobs, health)
        report.companies.append(health)

    known = {c.slug for c in report.companies}
    if state_dir.is_dir():
        report.orphan_state = sorted(
            p.stem for p in state_dir.glob("*.json") if p.stem not in known)

    report.filter_stats = stats_mod.load_stats()
    return report


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _pct(part: int, whole: int) -> str:
    return "-" if not whole else "%.1f%%" % (100.0 * part / whole)


def _rate_pct(rate: float) -> str:
    return "%.1f%%" % (100.0 * rate)


def _listing(companies: list[CompanyHealth], limit: int,
             line) -> list[str]:
    out = [line(c) for c in companies[:limit]]
    if len(companies) > limit:
        out.append("    ... and %d more" % (len(companies) - limit))
    return out


def format_text(report: CorpusReport, limit: int = 25) -> str:
    """The full report, for a terminal. Sections are ordered by how much a
    finding in them costs: a company that can never deliver is worse than one
    that is merely stale."""
    watched = report.watched
    lines = [
        "=" * 74,
        "CORPUS HEALTH - %s" % report.generated_at.strftime("%Y-%m-%d %H:%M UTC"),
        "=" * 74,
        "",
        "%d companies loaded, %d enabled, %d disabled, %d failed to load"
        % (len(report.companies), len(watched),
           len(report.companies) - len(watched), len(report.profile_errors)),
        "%d tracked postings, %d of them deliverable (%s)"
        % (report.tracked_postings, report.deliverable_postings,
           _pct(report.deliverable_postings, report.tracked_postings)),
        "",
    ]

    # --- 1. the headline detector -----------------------------------------
    lines += ["-" * 74,
              "1. STRUCTURALLY SILENT - companies that can never deliver",
              "-" * 74,
              "Proxy: no tracked posting whose stored title the role filter",
              "would pass. State does not record sends; this is what the data",
              "supports. See the module docstring.",
              ""]

    unseeded = report.unseeded
    unreadable = [c for c in watched if c.unreadable]
    empty = [c for c in report.silent if c.tracked == 0]
    off_family = [c for c in report.silent if c.off_family_only]

    lines.append("%d of %d enabled companies are structurally silent (%s)"
                 % (len(report.silent), len(watched),
                    _pct(len(report.silent), len(watched))))
    lines.append("  %d track no postings at all" % len(empty))
    lines.append("  %d track postings, none on-family" % len(off_family))
    lines.append("  %d have never been seeded (no state file)" % len(unseeded))
    lines.append("  %d have an unreadable state file" % len(unreadable))
    lines.append("")

    if empty:
        lines.append("  Zero tracked postings (board = the whole board, "
                     "before the Israel filter):")
        lines += _listing(sorted(empty, key=lambda c: c.slug), limit,
                          lambda c: "    %-28s %-16s board=%-6s%s"
                          % (c.slug, c.platform,
                             "?" if c.last_board_total is None
                             else c.last_board_total,
                             "" if c.zero_is_plausible
                             else "  [zero_is_plausible=false]"))
        lines.append("      board=0 means the endpoint returns nothing at "
                     "all - suspect the profile, not the hiring.")
        lines.append("      board=? means this fetcher does not report a "
                     "board size, or the company has not run since it began to.")
        lines.append("")
    if off_family:
        lines.append("  Tracking postings, none deliverable:")
        lines += _listing(sorted(off_family, key=lambda c: -c.tracked), limit,
                          lambda c: "    %-28s %-16s %d tracked, %d blocked, "
                          "%d non-job"
                          % (c.slug, c.platform, c.tracked, c.blocked,
                             c.non_job))
        lines.append("")
    for c in unreadable:
        lines.append("  UNREADABLE STATE: %s" % c.unreadable)
    if unseeded:
        lines.append("  Never seeded: %s"
                     % ", ".join(c.slug for c in unseeded[:limit]))
    lines.append("")

    # --- 1b. the board itself ---------------------------------------------
    suspicious_boards = sorted(
        (c for c in watched if c.implausible_board),
        key=lambda c: (c.last_board_total, c.slug))
    lines += ["-" * 74,
              "1b. IMPLAUSIBLY SMALL BOARDS - is this the right endpoint?",
              "-" * 74,
              "The only check here that reads a PRE-filter number. Every other",
              "one compares a company against itself, and a company pointed at",
              "the wrong board is perfectly consistent with itself forever.",
              ""]
    if suspicious_boards:
        lines.append("%d enabled companies fetch a board of %d postings or "
                     "fewer:" % (len(suspicious_boards), BOARD_TOO_SMALL))
        lines += _listing(suspicious_boards, limit,
                          lambda c: "    %-28s %-16s board=%-4d israel=%d"
                          % (c.slug, c.platform, c.last_board_total,
                             c.last_count))
        lines.append("")
        lines.append("    A real answer for a small startup and a wrong")
        lines.append("    endpoint for anyone else. Check the careers page.")
    else:
        lines.append("  None. Every company's board is large enough to be a")
        lines.append("  real careers board.")
    unmeasured = [c for c in watched
                  if c.seeded and c.last_board_total is None]
    if unmeasured:
        lines.append("")
        lines.append("  %d companies report no board size and cannot be "
                     "checked this way: %s"
                     % (len(unmeasured),
                        ", ".join(c.slug for c in unmeasured[:8])
                        + (" …" if len(unmeasured) > 8 else "")))
    lines.append("")

    # --- 2. duplicate ids --------------------------------------------------
    collisions = report.id_collisions()
    lines += ["-" * 74,
              "2. DUPLICATE JOB IDS - postings state cannot keep apart",
              "-" * 74,
              "last_count and len(jobs) are written together on a healthy run,",
              "so a gap between them means two postings shared one Job.id. The",
              "diff runs on that id, so the loser is permanently un-alertable.",
              ""]
    if collisions:
        lines.append("  *** %d companies are losing postings to id collisions "
                     "***" % len(collisions))
        for c in collisions[:limit]:
            lines.append("    %-28s %-16s fetched %d, stored %d, lost %d"
                         % (c.slug, c.platform, c.last_count, c.tracked,
                            c.id_collisions))
        lines.append("    Check what the profile maps to api.fields.id for "
                     "these.")
    else:
        lines.append("  None. Every company's stored id count matches what its "
                     "last")
        lines.append("  healthy fetch returned.")
    lines.append("")

    # --- 3. health gate ----------------------------------------------------
    lines += ["-" * 74, "3. HEALTH GATE", "-" * 74]
    failing = report.failing()
    below = report.below_floor()
    lines.append("%d companies carry consecutive_failures > 0" % len(failing))
    for c in failing[:limit]:
        lines.append("    %-28s %d consecutive, last_count=%d"
                     % (c.slug, c.consecutive_failures, c.last_count))
    lines.append("%d companies sit BELOW their own expected_min_jobs"
                 % len(below))
    lines.append("    (the gate cannot see these: it only fires when the")
    lines.append("     PREVIOUS run was at or above the floor)")
    for c in below[:limit]:
        lines.append("    %-28s last_count=%d, floor=%d"
                     % (c.slug, c.last_count, c.expected_min_jobs))
    lines.append("")

    # --- 3. staleness ------------------------------------------------------
    lines += ["-" * 74, "4. STALENESS", "-" * 74]
    stale_run = report.stale_success()
    stale_ver = report.stale_verification()
    lines.append("%d companies have no successful fetch in the last %dh"
                 % (len(stale_run), STALE_SUCCESS_HOURS))
    for c in stale_run[:limit]:
        hours = _hours_since(c.last_success, report.generated_at)
        lines.append("    %-28s %s"
                     % (c.slug, "never" if hours is None else "%.0fh ago" % hours))
    lines.append("%d profiles were last verified over %d days ago"
                 % (len(stale_ver), STALE_VERIFIED_DAYS))
    for c in sorted(stale_ver, key=lambda c: (c.verified_on or date.min))[:limit]:
        lines.append("    %-28s verified_on=%s"
                     % (c.slug, c.verified_on or "missing"))

    churn = report.no_churn()
    eligible = report.churn_eligible()
    lines.append("")
    lines.append("%d of %d companies old enough to judge (>= %dd since seed) "
                 "have" % (len(churn), len(eligible), NO_CHURN_MIN_AGE_DAYS))
    lines.append("detected nothing new since seeding. %d companies are too "
                 "young"
                 % (len([c for c in report.watched if c.seeded and c.tracked])
                    - len(eligible)))
    lines.append("for this metric to mean anything and are not counted.")
    lines.append("    A watchlist, not a verdict - a small board goes weeks")
    lines.append("    without a new req. Ranked by board size, because a big")
    lines.append("    board that never moves is the suspicious one.")
    for c in churn[:limit]:
        age = _hours_since(c.seeded_at, report.generated_at)
        lines.append("    %-28s %-16s %d tracked, %.0fd since seed"
                     % (c.slug, c.platform, c.tracked, (age or 0) / 24))
    lines.append("")

    # --- 4. distribution ---------------------------------------------------
    lines += ["-" * 74, "5. DISTRIBUTION", "-" * 74,
              "%-16s %5s %8s %8s %8s %8s %7s"
              % ("platform", "cos", "tracked", "target", "unknown", "silent",
                 "silent%")]
    for platform, members in report.by_platform().items():
        silent = sum(1 for c in members if c.structurally_silent)
        lines.append("%-16s %5d %8d %8d %8d %8d %7s"
                     % (platform, len(members),
                        sum(c.tracked for c in members),
                        sum(c.target for c in members),
                        sum(c.unknown for c in members),
                        silent, _pct(silent, len(members))))

    flagged = report.underperforming_platforms()
    lines.append("")
    if flagged:
        lines.append("  *** PLATFORM SILENCE OUTLIER ***")
        for platform, rate, corpus_rate in flagged:
            lines.append("    %s is silent on %s of its boards, against %s "
                         "across the corpus."
                         % (platform, _rate_pct(rate), _rate_pct(corpus_rate)))
        lines.append("    One platform wrong together is what a fetcher or")
        lines.append("    platform-profile defect looks like from here.")
    else:
        lines.append("  No platform's silent rate is %.0fx the corpus rate."
                     % PLATFORM_SILENCE_MULTIPLE)

    counts = sorted((c.tracked for c in watched), reverse=True)
    if counts:
        lines.append("")
        lines.append("  Postings per company: max %d, median %d, "
                     "0 postings at %d companies"
                     % (counts[0], counts[len(counts) // 2],
                        sum(1 for n in counts if n == 0)))
        buckets = [("0", lambda n: n == 0), ("1-4", lambda n: 1 <= n <= 4),
                   ("5-19", lambda n: 5 <= n <= 19),
                   ("20+", lambda n: n >= 20)]
        lines.append("  " + "  ".join(
            "%s: %d" % (label, sum(1 for n in counts if test(n)))
            for label, test in buckets))
    lines.append("")

    # --- 5. filter funnel --------------------------------------------------
    lines += ["-" * 74, "6. FILTER FUNNEL", "-" * 74]
    lines += _format_funnel(report)

    if report.profile_errors:
        lines += ["", "-" * 74, "PROFILE ERRORS", "-" * 74]
        lines += ["  " + e for e in report.profile_errors[:limit]]
    if report.orphan_state:
        lines += ["", "-" * 74, "ORPHAN STATE FILES", "-" * 74,
                  "  state/seen/<slug>.json with no profile - a renamed or",
                  "  deleted company whose history is still on disk:",
                  "  " + ", ".join(report.orphan_state[:limit])]

    return "\n".join(lines)


def _format_funnel(report: CorpusReport) -> list[str]:
    """filter_stats.json, plus the one per-company view the data supports.

    The counters are corpus-wide and carry no slug, so "which company is
    responsible for the undetermined pile" is not answerable from them. What IS
    answerable is which companies can never produce a number at all, because
    their profile has no way to reach a description - see
    CorpusReport.undeterminable."""
    stored = report.filter_stats or {}
    totals = stored.get("totals") or {}
    lines = []

    experience = totals.get("experience") or {}
    if experience:
        determined = (experience.get("passed_with_number", 0)
                      + experience.get("rejected_with_number", 0))
        undetermined = (experience.get("undetermined", 0)
                        + experience.get("undetermined_signals", 0))
        lines.append("experience (lifetime):")
        for key in stats_mod.COUNTER_NAMES:
            lines.append("    %-24s %d" % (key, experience.get(key, 0)))
        lines.append("    determination rate:    %s of the postings that"
                     " reached the detail layer"
                     % _pct(determined, determined + undetermined))
        lines.append("    %d delivered fail-open with no number found."
                     % undetermined)
    else:
        lines.append("experience: no lifetime counters recorded yet.")

    role = totals.get("role") or {}
    if role:
        lines.append("role (lifetime):")
        for key in stats_mod.ROLE_COUNTER_NAMES:
            lines.append("    %-24s %d" % (key, role.get(key, 0)))

    undeterminable = report.undeterminable()
    lines.append("")
    lines.append("Structurally undeterminable: %d companies have no "
                 "detail_fetch," % len(undeterminable))
    lines.append("so every posting they produce is 'undetermined' by "
                 "construction")
    lines.append("(correct fail-open behaviour, not a defect - but it is where")
    lines.append("the undetermined pile comes from):")
    for c in undeterminable[:25]:
        lines.append("    %-28s %-16s %d tracked"
                     % (c.slug, c.platform, c.tracked))
    return lines


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
#
# A compact version, for /health. It has to fit one message and be readable on
# a phone, so it carries only the numbers that would make someone go and look:
# how many companies can never deliver, what is failing, what is stale. The
# full listing stays in the CLI report - a command that dumps 368 lines is a
# command nobody reads twice.
#
# Hebrew here is product content for the user, exactly as in commands.py, not a
# violation of the English-comments rule.

def format_telegram(report: CorpusReport) -> str:
    from notifier import escape_mdv2

    watched = report.watched
    silent = report.silent
    empty = [c for c in silent if c.tracked == 0]
    off_family = [c for c in silent if c.off_family_only]
    failing = report.failing()
    below = report.below_floor()
    stale = report.stale_success()

    lines = [
        "\U0001FA7A *בריאות המעקב*", "",
        "חברות פעילות: %d" % len(watched),
        "משרות במעקב: %d \\(%s רלוונטיות\\)"
        % (report.tracked_postings,
           escape_mdv2(_pct(report.deliverable_postings,
                            report.tracked_postings))),
        "",
        "\U0001F507 *לא יכולות לשלוח כלום:* %d" % len(silent),
        "   ללא משרות כלל: %d" % len(empty),
        "   משרות מחוץ לתחום בלבד: %d" % len(off_family),
        "",
        "🔀 מזהים כפולים \\(משרות שנעלמות\\): %d"
        % sum(c.id_collisions for c in watched),
        # The only pre-filter number in the summary, and the one that answers
        # "are we even looking at the right page" - see
        # CompanyHealth.implausible_board.
        "\U0001F50E לוחות קטנים מדי \\(אולי כתובת שגויה\\): %d"
        % len([c for c in watched if c.implausible_board]),
        "⚠️ כשלים רצופים: %d" % len(failing),
        "\U0001F4C9 מתחת לרצפת המשרות: %d" % len(below),
        "\U0001F553 ללא שליפה מוצלחת מעל %dh: %d"
        % (STALE_SUCCESS_HOURS, len(stale)),
    ]

    flagged = report.underperforming_platforms()
    if flagged:
        lines.append("")
        lines.append("\U0001F6A8 *פלטפורמה חשודה:*")
        for platform, rate, corpus_rate in flagged:
            lines.append("   %s — %s מהלוחות שותקים"
                         % (escape_mdv2(platform),
                            escape_mdv2(_rate_pct(rate))))

    worst = sorted(silent, key=lambda c: -c.tracked)[:10]
    if worst:
        lines.append("")
        # Deliberately NOT wrapped in _italics_. Every slug here is free text
        # that must be escaped, while an emphasis delimiter is the one `_` in
        # the message that must NOT be - which makes "is this message safe to
        # send" unanswerable by inspection. Telegram rejects the entire reply
        # with a 400 over one stray character, so /health would fail silently
        # and permanently. Plain text keeps the rule absolute: everything is
        # escaped except the `*` headings.
        lines.append(escape_mdv2(", ".join(c.slug for c in worst)))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Corpus-health report. Read-only: it never writes to "
                    "state/ or profiles/.")
    parser.add_argument("--limit", type=int, default=25, metavar="N",
                        help="how many companies to list per section "
                             "(default 25)")
    parser.add_argument("--telegram", action="store_true",
                        help="render the compact /health summary instead")
    args = parser.parse_args(argv)

    report = collect()
    text = (format_telegram(report) if args.telegram
            else format_text(report, limit=args.limit))
    # The corpus is English but a title or a name need not be. Encoding
    # errors here would be a report that cannot print itself.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
