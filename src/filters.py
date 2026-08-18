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
import roles
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

    def counter_for(self, verdict: Verdict, prescreened: bool) -> str:
        """Which stats bucket this verdict belongs in.

        A hook rather than a fixed mapping in _count, because the bucket names
        are the filter's own vocabulary: "passed_with_number" describes the
        experience filter and means nothing for a role filter. The default is
        the experience filter's original mapping, so a filter that doesn't
        override this behaves exactly as before."""
        if prescreened and not verdict.passed:
            return "rejected_by_title"
        if verdict.confidence == "certain":
            return "passed_with_number" if verdict.passed else "rejected_with_number"
        if verdict.confidence == "seniority_signals":
            return "undetermined_signals"
        return "undetermined"

    def tag_for(self, job: Job, verdict: Verdict) -> str | None:
        """Short user-facing label for an alert line, or None for no label.
        Built by the filter because only the filter knows what its own
        verdict means; rendered by the notifier, which doesn't."""
        return None


# ---------------------------------------------------------------------------
# H. Title pre-check - REJECT-ONLY
# ---------------------------------------------------------------------------

# Seniority words that decide a rejection with zero requests.
#
# The second row was added 2026-08-18 after measuring the live corpus: each
# entry is followed by how many postings it caught that the original list let
# through. "leader" is the sharpest example and the reason to be careful with
# whole-word matching - "lead" does NOT match inside "Leader", so 33 "Team
# Leader" postings were arriving as junior-eligible.
#
# Words deliberately NOT here: "specialist" (30 postings, and it is a level,
# not a seniority - "Technical Support Specialist - Student Position"),
# "owner" ("Product Owner" is routinely a mid role), and "controller" /
# "counsel", which are senior AND off-target and are owned by roles.py so each
# list does one job.
_SENIOR_TITLE_WORDS = [
    "senior", "sr", "lead", "staff", "principal", "architect",
    "manager", "director", "head of", "vp",
    "leader",          # 33  "Back-End Team Leader", "NOC Team Leader"
    "experienced",     # 20  "Experienced Algorithm Validation Engineer"
    "expert",          # 15  "GenAI CBRNE Cyber Expert", "Tableau Expert"
    "chief", "ciso", "cfo", "coo", "cio", "cmo", "cro",   # "CISO", "COO"
    "svp", "evp", "distinguished", "fellow", "deputy", "veteran", "seasoned",
]

# A junior signal OVERRIDES every word above.
#
# The one false negative in the bot's entire delivered history was mprest's
# "Junior Project Manager", rejected for containing "manager". A posting that
# says junior/intern/student/graduate outright is stating its own level, and
# no seniority noun elsewhere in the title outranks that. This can only ever
# ADD postings, which is the direction this project errs in.
_JUNIOR_TITLE_WORDS = [
    "junior", "jr", "intern", "internship", "student", "graduate",
    "entry level", "trainee", "apprentice", "cadet",
    "ג׳וניור", "סטודנט", "סטודנטית", "מתמחה", "מתמחה", "התמחות",
]

# Titles where a seniority word names an ORGANISATION, not the role's level.
# Mobileye posts "Algorithm Researcher - Autonomous Driving (CTO Group)":
# a research role in the CTO's group, not a C-level job.
_ORG_NOT_LEVEL = ["cto group", "cto office", "ceo office", "cto s office"]


def _normalize_title(title: str) -> str:
    """Lowercases and reduces punctuation to spaces, then pads with spaces so
    every lookup below is a whole-word match. Substring matching would reject
    "Salesforce Administrator" for containing "sr" and is not an option."""
    text = re.sub(r"[^0-9a-z֐-׿]+", " ", (title or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    return f" {text} "


def title_looks_senior(title: str) -> bool:
    """Reject-only. A senior-sounding title is decisive; a junior-sounding
    one proves NOTHING about the requirement and must not short-circuit the
    experience check.

    ~35% of postings labelled "entry-level" still demand 3+ years (LinkedIn
    analysis), so "Junior"/"Intern"/"Student"/"Graduate" still go on to the
    detail fetch and are still evaluated on their stated number like any other
    posting. What a junior word does here is narrower and only ever additive:
    it stops a seniority noun elsewhere in the same title from rejecting the
    posting outright ("Junior Project Manager"). The experience filter then
    has its usual say."""
    normalized = _normalize_title(title)
    if any(f" {word} " in normalized for word in _JUNIOR_TITLE_WORDS):
        return False
    if any(phrase in normalized for phrase in _ORG_NOT_LEVEL):
        return False
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


# ---------------------------------------------------------------------------
# The role filter
# ---------------------------------------------------------------------------

class RoleFilter(Filter):
    """Suppresses postings outside the user's job families - see roles.py for
    the families, the lists, and why it is a blocklist.

    *** Why it is entirely a prescreen ***

    It decides from the title alone, so it never wants a description and never
    costs a request. Placed before ExperienceFilter in the registry, it also
    removes its rejects from the detail fetch entirely: an off-target posting
    costs nothing at all, rather than costing a page fetch to establish an
    experience number nobody will read.

    The three outcomes match roles.classify, plus one more:
        non-job card  -> REJECT   (an evergreen "we're always hiring" tile)
        blocked domain-> REJECT
        target family -> PASS
        unrecognised  -> PASS, flagged      <- fail-open, and the point

    `send_unknown=False` inverts that last line. It exists as the escape hatch
    strict mode is for the experience filter, and like strict it is OFF by
    default, because turning it on trades a quieter inbox for silently losing
    the roles whose titles name no technology - "DFIR", "CyOps Analyst",
    "InfoSec & SecOps" are all real, on-target, and all land in that bucket.
    """

    name = "role"

    def __init__(self, send_unknown: bool = True) -> None:
        self.send_unknown = bool(send_unknown)

    def prescreen(self, job: Job) -> Verdict | None:
        if roles.is_non_job(job.title):
            return Verdict(passed=False, reason="not a job posting",
                           confidence="certain")
        classification, term = roles.classify(job.title)
        if classification == "blocked":
            return Verdict(passed=False, reason=f"off-target role ({term})",
                           confidence="certain")
        if classification == "unknown":
            return Verdict(passed=self.send_unknown,
                           reason="role could not be classified",
                           confidence="unknown")
        return Verdict(passed=True, reason=f"target role ({term})",
                       confidence="certain")

    def evaluate(self, job: Job) -> tuple[Job, Verdict]:
        """Nothing more to learn from a description - the prescreen is the
        whole filter. Returning the same verdict keeps the chain's contract
        without a second classification."""
        return job, self.prescreen(job)

    def counter_for(self, verdict: Verdict, prescreened: bool) -> str:
        """Buckets named for what this filter actually decides. The
        experience filter's names ("passed_with_number") would be nonsense
        here, and the whole value of the counters is telling working from
        broken at a glance."""
        if verdict.confidence == "unknown":
            return "unclassified_sent" if verdict.passed else "unclassified_dropped"
        return "target_role" if verdict.passed else "off_target"

    def tag_for(self, job: Job, verdict: Verdict) -> str | None:
        """Two independent labels, either or both.

        The temporary label lives here rather than in its own filter because
        it is read off the title, like everything else this filter does, and a
        filter that only ever tags would need a registry entry, a settings
        block and a toggle to express "add three words to a line"."""
        labels = []
        if verdict.confidence == "unknown":
            labels.append("❓ תפקיד לא מזוהה")
        if roles.is_temporary(job.title):
            labels.append("⏳ משרה זמנית/חלופת לידה")
        return " · ".join(labels) if labels else None


# The registry a filter must be listed in to exist. Adding a filter = adding
# a builder here plus a default block in settings.DEFAULTS.
#
# ORDER MATTERS: dict order is the chain order, and the role filter runs first
# on purpose. It is free (title only), so anything it rejects never reaches the
# experience filter's detail fetch.
_BUILDERS = {
    "role": lambda cfg: RoleFilter(send_unknown=cfg.get("send_unknown", True)),
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


def _count(stats: RunStats, job_filter: "Filter", verdict: Verdict,
           prescreened: bool) -> None:
    """Records a verdict in whichever bucket its own filter names for it.

    Only a REJECTING prescreen counts as "rejected_by_title" in the default
    mapping: counting a PASSING prescreen as a rejection would quietly corrupt
    the only numbers that show whether the filter works at all. The role filter
    does return passing prescreen verdicts, which is exactly why the mapping
    became the filter's own business."""
    stats.record(job_filter.name,
                 job_filter.counter_for(verdict, prescreened))


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
    decided_in_prescreen: dict[str, set[str]] = {}
    for job in jobs:
        rejected = False
        for job_filter in chain:
            verdict = job_filter.prescreen(job)
            if verdict is None:
                continue
            _count(stats, job_filter, verdict, prescreened=True)
            if not verdict.passed:
                rejected = True
                break        # first rejection wins; no point asking the rest
            # A filter that decided the job HERE must not count it a second
            # time in the evaluation pass below - the role filter's prescreen
            # is its whole verdict, so without this every surviving job would
            # be tallied twice and the counters would read double.
            decided_in_prescreen.setdefault(job.id, set()).add(job_filter.name)
        if not rejected:
            survivors_of_prescreen.append(job)

    # The detail fetch, gated twice: some filter must want descriptions AND
    # the profile must actually offer a way to reach them.
    if survivors_of_prescreen and any(f.wants_description() for f in chain):
        survivors_of_prescreen = detail.enrich(
            survivors_of_prescreen, profile, budget)

    results: list[tuple[Job, str | None]] = []
    for job in survivors_of_prescreen:
        labels: list[str] = []
        already = decided_in_prescreen.get(job.id, set())
        passed = True
        for job_filter in chain:
            job, verdict = job_filter.evaluate(job)
            if job_filter.name not in already:
                _count(stats, job_filter, verdict, prescreened=False)
            label = job_filter.tag_for(job, verdict)
            if label:
                labels.append(label)
            if not verdict.passed:
                passed = False
                break
        if passed:
            # Every filter with something to say gets to say it. Joined rather
            # than overwritten because the labels are independent facts about
            # the job ("role unrecognised" AND "maternity cover"), and the old
            # last-one-wins rule silently dropped whichever came first.
            results.append((job, " · ".join(labels) if labels else None))

    return results
