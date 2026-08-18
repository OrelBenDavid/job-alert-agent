# -*- coding: utf-8 -*-
"""
The filter chain: a generic layer between diffing and notification.

*** Where this sits, and why it can't move ***

    fetch -> dedupe by id -> diff vs state -> NEW jobs
          -> write ALL new ids to state          <- BEFORE filtering
          -> title pre-check -> detail fetch -> filter chain
          -> notify survivors

The state write happens first, and that ordering is not negotiable. If
rejected jobs were held out of state, every rejected job would look new on
every subsequent run and its detail page would be re-fetched forever. State
means "everything ever seen"; filtering is presentation only.

The accepted consequence: turning a filter OFF does not retroactively
deliver jobs that were rejected while it was on. That is deliberate, and
there is no replay mechanism by design.

*** Generic, not experience-specific ***

The experience filter is simply the chain's first member. A future filter
(salary, stack) implements the same three-method interface and plugs in
without the notifier, the runner, or this module's pipeline changing at all.

A disabled filter is never constructed, so it triggers no work of any kind -
with the experience filter off there is no detail fetch, and a run costs
exactly what it cost before this patch existed.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import dataclasses
import re
from dataclasses import dataclass
from typing import Literal

import detail
from experience import read_experience
from models import Job
from settings import load_settings
from stats import RunStats


@dataclass(frozen=True)
class Verdict:
    """One filter's answer about one job.

    confidence is separate from `passed` because the two carry different
    information: ~70% of postings state no number at all, so a plain
    pass/reject flag would collapse "verified junior" and "no idea" into the
    same green tick and make the tick meaningless."""
    passed: bool
    reason: str
    confidence: Literal["certain", "seniority_signals", "unknown"]


class Filter:
    """Base class / interface. Subclasses override what they need.

    evaluate() returns the job as well as the verdict so a filter can attach
    what it derived (parsed years, parsed salary) to the Job for the notifier
    to display, without the pipeline knowing what any of it means."""

    name = ""

    def wants_description(self) -> bool:
        """Whether this filter needs Job.description populated. Consulted
        before any detail fetch happens, so a chain of filters that don't
        need descriptions never triggers one."""
        return False

    def prescreen(self, job: Job) -> Verdict | None:
        """A verdict reachable without a description, or None for "no
        opinion yet". This is what makes the title pre-check free: a job
        rejected here never costs a request."""
        return None

    def evaluate(self, job: Job) -> tuple[Job, Verdict]:
        raise NotImplementedError

    def tag_for(self, job: Job, verdict: Verdict) -> str | None:
        """Short user-facing label for an alert line, or None for no label.
        Built by the filter because only the filter knows what its own
        verdict means; rendered by the notifier, which doesn't."""
        return None


# ---------------------------------------------------------------------------
# H. Title pre-check - REJECT-ONLY
# ---------------------------------------------------------------------------

# Seniority words that decide a rejection with zero requests.
_SENIOR_TITLE_WORDS = [
    "senior", "sr", "lead", "staff", "principal", "architect",
    "manager", "director", "head of", "vp",
]


def _normalize_title(title: str) -> str:
    """Lowercases and reduces punctuation to spaces, then pads with spaces so
    every lookup below is a whole-word match. Substring matching would reject
    "Salesforce Administrator" for containing "sr" and is not an option."""
    text = re.sub(r"[^0-9a-z֐-׿]+", " ", (title or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    return f" {text} "


def title_looks_senior(title: str) -> bool:
    """Reject-only. A senior-sounding title is decisive; a junior-sounding
    one proves NOTHING and must not short-circuit anything.

    ~35% of postings labelled "entry-level" still demand 3+ years (LinkedIn
    analysis), so "Junior"/"Intern"/"Student"/"Graduate" go on to the detail
    fetch like any other posting. That costs a few requests and buys
    correctness."""
    normalized = _normalize_title(title)
    return any(f" {word} " in normalized for word in _SENIOR_TITLE_WORDS)


# ---------------------------------------------------------------------------
# The experience filter
# ---------------------------------------------------------------------------

def _hebrew_years(years: float) -> str:
    """Hebrew is the user's language, so the alert labels are Hebrew product
    content (same convention as the Telegram command replies)."""
    if years == 1:
        return "שנה"
    if years == 2:
        return "שנתיים"
    return f"{years:g} שנים"


class ExperienceFilter(Filter):
    """Suppresses postings whose stated MINIMUM required experience exceeds
    the threshold.

    The decision table is hard, with no grey zone:
        min_years is None  -> PASS, flagged        (fail-open)
        min_years <= max    -> PASS
        min_years >  max    -> REJECT
    """

    name = "experience"

    def __init__(self, max_years: float = 1.0, strict: bool = False) -> None:
        self.max_years = float(max_years)
        self.strict = bool(strict)

    def wants_description(self) -> bool:
        return True

    def prescreen(self, job: Job) -> Verdict | None:
        if title_looks_senior(job.title):
            return Verdict(passed=False, reason="senior title",
                           confidence="certain")
        return None      # everything else, junior-sounding included, goes on

    def evaluate(self, job: Job) -> tuple[Job, Verdict]:
        reading = read_experience(job.description)
        job = dataclasses.replace(job, min_years_exp=reading.min_years)

        if reading.min_years is None:
            # Undetermined - the MAJORITY outcome, not an edge case. Passes
            # unless strict mode was explicitly turned on.
            confidence = ("seniority_signals" if reading.has_seniority_signals
                          else "unknown")
            return job, Verdict(
                passed=not self.strict,
                reason="no stated experience requirement",
                confidence=confidence)

        if reading.min_years > self.max_years:
            return job, Verdict(
                passed=False,
                reason=f"requires {reading.min_years:g} years",
                confidence="certain")

        return job, Verdict(
            passed=True, reason=f"requires {reading.min_years:g} years",
            confidence="certain")

    def tag_for(self, job: Job, verdict: Verdict) -> str | None:
        """The three-level tag. All three still send - this only sorts the
        undetermined pile so it stays skimmable."""
        if verdict.confidence == "seniority_signals":
            return "🔶 לא צוין מספר, יש סימני ותק"
        if verdict.confidence == "unknown":
            return "⚠️ לא צוינה דרישת ניסיון"
        if job.min_years_exp == 0:
            return "✅ לא נדרש ניסיון"
        if job.min_years_exp is None:
            return None
        return f"✅ ניסיון: עד {_hebrew_years(job.min_years_exp)}"


# The registry a filter must be listed in to exist. Adding a filter = adding
# a builder here plus a default block in settings.DEFAULTS.
_BUILDERS = {
    "experience": lambda cfg: ExperienceFilter(
        max_years=cfg.get("max_years", 1.0), strict=cfg.get("strict", False)),
}


def build_chain(settings: dict | None = None) -> list[Filter]:
    """Constructs the enabled filters, in registry order.

    A disabled filter is not constructed at all - it isn't built and then
    skipped. That is what guarantees a disabled filter costs nothing rather
    than merely deciding nothing."""
    settings = settings if settings is not None else load_settings()
    chain: list[Filter] = []
    for name, builder in _BUILDERS.items():
        cfg = settings.get(name) or {}
        if cfg.get("enabled"):
            chain.append(builder(cfg))
    return chain


def _count(stats: RunStats, filter_name: str, verdict: Verdict,
           prescreened: bool) -> None:
    """Maps a verdict onto the counter that describes it."""
    if prescreened and not verdict.passed:
        # Only a REJECTING prescreen is "rejected by title". No filter
        # currently returns a passing prescreen verdict, but counting one as a
        # rejection would quietly corrupt the only numbers that show whether
        # the filter works at all.
        stats.record(filter_name, "rejected_by_title")
    elif verdict.confidence == "certain":
        stats.record(filter_name,
                     "passed_with_number" if verdict.passed
                     else "rejected_with_number")
    elif verdict.confidence == "seniority_signals":
        stats.record(filter_name, "undetermined_signals")
    else:
        stats.record(filter_name, "undetermined")


def collapse_duplicate_titles(jobs: list[Job]) -> list[Job]:
    """Keeps one job per (company, normalized title) within a single batch.

    *** Why this is display-only, and why that is enough ***

    Several boards publish one posting PER CITY for a single opening. Comeet
    even admits it in the id - "B6.D6A" and "B6.D6A-9D.50A" are the same
    MLOps role in Netanya and Tel Aviv - and Greenhouse/Mobileye issue
    genuinely distinct uuids per site, so the diff cannot tell them apart and
    must not try: they ARE different ids, and treating them as one would mean
    inventing an identity the ATS does not have.

    Measured across the corpus: 52 redundant rows, 3.9% of postings, 23 of
    them at one company. The user saw "MLOps Engineer" twice in one batch.

    So the duplicates stay in state - every id is still recorded as seen,
    exactly as before, and nothing is ever re-detected - and only the ALERT
    collapses. The consequence is deliberate and small: if the same role's
    second city appears in a LATER run than the first, it is a new id in a
    new batch and gets its own line. Persisting a title across runs to stop
    that would be a second identity key living alongside the id, which is the
    thing this project's whole diff design refuses to have.

    The kept row is the first in profile order, so the choice is stable."""
    seen: set[tuple[str, str]] = set()
    out: list[Job] = []
    for job in jobs:
        key = (job.company, re.sub(r"\s+", " ", (job.title or "").strip().lower()))
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def run_chain(jobs: list[Job], profile, chain: list[Filter],
              stats: RunStats | None = None,
              budget: "detail.DetailBudget | None" = None
              ) -> list[tuple[Job, str | None]]:
    """new jobs -> [(surviving job, display tag or None)].

    Four passes, in this order for cost reasons:
      0. collapse per-city duplicates - free, and shrinks every pass below it
      1. prescreen every job   - free, no requests
      2. one detail fetch for whatever survived, and only if some enabled
         filter actually wants descriptions
      3. full evaluation

    `budget` is the run-wide detail-fetch allowance, shared across every
    company so the cap means what its name says.

    An empty chain returns every job untagged, which is exactly the
    pre-patch behaviour."""
    stats = stats if stats is not None else RunStats()

    if not chain:
        return [(job, None) for job in jobs]

    # Before the prescreen, so a duplicate never costs a detail fetch either.
    jobs = collapse_duplicate_titles(jobs)

    survivors_of_prescreen: list[Job] = []
    for job in jobs:
        rejected = False
        for job_filter in chain:
            verdict = job_filter.prescreen(job)
            if verdict is None:
                continue
            _count(stats, job_filter.name, verdict, prescreened=True)
            if not verdict.passed:
                rejected = True
                break        # first rejection wins; no point asking the rest
        if not rejected:
            survivors_of_prescreen.append(job)

    # The detail fetch, gated twice: some filter must want descriptions AND
    # the profile must actually offer a way to reach them.
    if survivors_of_prescreen and any(f.wants_description() for f in chain):
        survivors_of_prescreen = detail.enrich(
            survivors_of_prescreen, profile, budget)

    results: list[tuple[Job, str | None]] = []
    for job in survivors_of_prescreen:
        tag: str | None = None
        passed = True
        for job_filter in chain:
            job, verdict = job_filter.evaluate(job)
            _count(stats, job_filter.name, verdict, prescreened=False)
            label = job_filter.tag_for(job, verdict)
            if label:
                tag = label
            if not verdict.passed:
                passed = False
                break
        if passed:
            results.append((job, tag))

    return results
