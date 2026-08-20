# -*- coding: utf-8 -*-
"""
State management per company: which job ids have already been seen, how
many jobs were successfully fetched last time, and how many consecutive
failures the company has accumulated.

This is where the project's most important principle is enforced: if a run
returns 0 jobs after a previous run returned a healthy count, that's a
failure, not "no jobs". State is not overwritten, and the alert that goes
out is a maintenance alert, not "0 new jobs".

That suspicion is held for a bounded number of runs, never indefinitely -
see the two ACCEPT_AFTER constants. A gate with no exit is not a safer gate:
it either blocks real postings (a partial collapse) or repeats one alert
every cron interval until the user stops reading them (a total zero).
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from models import Job

STATE_DIR = Path(__file__).resolve().parent.parent / "state" / "seen"
FAILURE_ALERT_THRESHOLD = 2   # only alert after 2 consecutive failures, not
                               # 1 - so a one-off network hiccup doesn't flood

@dataclass
class RunResult:
    """The result of processing a single company in one run."""
    slug: str
    status: str                       # "ok" | "empty_suspicious" | "error"
    new_jobs: list[Job] = field(default_factory=list)
    total_fetched: int = 0
    message: str = ""                 # detail for an error/warning, for
                                       # logging and diagnostics


class StateUnreadable(Exception):
    """A company's state file exists but can't be parsed.

    A distinct type, and deliberately NOT folded into "empty state", because
    the two must lead to opposite actions. Empty state means "never seeded"
    and run.py skips the company with a seed-gap notice. An unreadable file
    means the ids ARE on disk and just can't be read right now - treating that
    as "never seeded" would invite a re-seed that silently swallows every
    currently-open posting as already-known, which is the one outcome this
    project is built to prevent.

    It also must not be a bare json.JSONDecodeError escaping into run.py: that
    read happens in a loop before any company is fetched, so one damaged file
    took the entire run down - every company, no alerts - which is exactly the
    failure mode settings.py and stats.py already guard their own reads
    against."""


def _state_path(slug: str) -> Path:
    return STATE_DIR / f"{slug}.json"


def _empty_state() -> dict:
    return {"last_success": None, "last_count": 0,
            "consecutive_failures": 0, "jobs": {}}


def load_state(slug: str) -> dict:
    """Loads a company's existing state. A brand-new company (never seeded)
    gets empty state back - that's expected, and run.py treats it as a case
    that needs a manual seed, not a normal run.

    Missing keys are filled from the empty state rather than read straight
    off disk: an older or hand-edited file lacking `last_count` used to raise
    KeyError from inside the health gate, mid-run, after the fetch was already
    paid for."""
    path = _state_path(slug)
    if not path.exists():
        return _empty_state()

    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise StateUnreadable(f"{slug}: {path.name} could not be read ({e})") from e
    if not isinstance(stored, dict):
        raise StateUnreadable(
            f"{slug}: {path.name} is valid JSON but not an object")

    state = _empty_state()
    state.update(stored)
    if not isinstance(state.get("jobs"), dict):
        raise StateUnreadable(f"{slug}: {path.name} has no readable 'jobs' map")
    return state


def _write_state(slug: str, state: dict) -> None:
    """Atomic write: write to a temp file, then replace - so a crash mid-
    write (e.g. the runner gets killed on timeout) never leaves a half-
    written state file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(slug)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# *** The relative collapse check ***
#
# A count that fell to under this fraction of the last healthy run is treated
# as a collapse regardless of what the profile claims about itself. This is
# the half of the gate that SCALES: `expected_min_jobs` is a number a human
# picked on one day, and at a hundred companies those numbers go stale faster
# than anyone maintains them. A ratio needs no maintenance and follows a
# company as it grows.
#
# 0.4 rather than something tighter because the window is three hours. A
# company shedding 60% of its open roles inside one cron interval is not
# ordinary churn on any board this project has measured.
_COLLAPSE_RATIO = 0.4

# ...but only once there is enough of a baseline for the ratio to mean
# anything. On a board with 4 open roles, one closing is 25% and completely
# normal; the absolute floor and the zero check cover the small companies.
_COLLAPSE_MIN_BASELINE = 10

# How many consecutive gate hits to hold out for before accepting the new
# count as normal. Every kind of hit has one - see below for why they differ.
#
# This escape hatch is not optional. Freezing state on a PARTIAL collapse
# actively loses jobs: process_company returns no new jobs while frozen, so a
# false positive silently stops detecting real postings at that company, which
# is precisely the "quiet while looking healthy" failure the gate exists to
# prevent. Holding for three runs (~9 hours) reports the drop twice - crossing
# the alert threshold - and then resumes on its own rather than waiting for a
# human who may be asleep.
PARTIAL_COLLAPSE_ACCEPT_AFTER = 3

# *** The total-zero case needs one too - added 2026-08-19 ***
#
# It used to have none, on the reasoning that freezing state on a zero is free
# because there are no jobs to miss. That is true about JOBS and false about
# the alert: should_alert_failure fires on every run once the counter is past
# FAILURE_ALERT_THRESHOLD, so a company that has genuinely closed its last
# Israeli role sends the identical maintenance alert every three hours,
# forever, with no path back to healthy that does not involve a human editing
# a JSON file.
#
# Observed: panaya's single Israel-relevant posting was filled, and the bot
# sent the same "got 0 jobs after the previous run returned 1" alert on six
# consecutive runs. Its endpoint was verified live and is perfectly healthy -
# it returns four postings, in Brazil, the USA and Germany. There is no
# breakage to fix, and no number a human could have set that would have made
# the alert stop.
#
# That is worse than it sounds, because the cost is not noise, it is the
# gate's credibility: a maintenance alert that cries wolf every three hours is
# one the user stops reading, and the next one will be real.
#
# LONGER than the partial threshold, deliberately - the two costs are genuinely
# different, and holding a zero really is the cheaper mistake. Six runs is
# ~18 hours, so a transient outage at one company never reaches it, while a
# real closure resolves itself inside a day.
#
# What accepting costs, stated plainly: `jobs` is emptied, so if this WAS a
# breakage rather than a real zero, every posting it stopped returning is now
# un-seen and re-alerts once the fetch is fixed. That is the same trade the
# partial path already makes, and it errs toward re-sending rather than toward
# silence.
TOTAL_ZERO_ACCEPT_AFTER = 6


def _collapse_suspicion(slug: str, count: int, state: dict,
                        profile) -> tuple[str, int]:
    """The health gate's decision: (message, accept_after). "" means healthy.

    `accept_after` is how many consecutive runs this kind of hit is held for
    before the new count is accepted as normal. Returned per branch rather than
    read from one constant because the three hits do not cost the same thing to
    hold - see the two ACCEPT_AFTER constants above.

    Three thresholds, because a listing can break in more than one shape and
    only the first used to be caught:

    1. A TOTAL zero after any healthy count. The original gate, and what a
       dead job_selector looks like.

    2. A PARTIAL collapse below `health.expected_min_jobs`, from a run that
       was itself above that floor. Good at SLOW decay - a company drifting
       down over months never trips a run-to-run comparison - and dependent on
       someone having chosen a sane number.

    3. A PARTIAL collapse below `_COLLAPSE_RATIO` of the last healthy count.
       Good at SUDDEN breakage and needs no per-profile number at all, which
       is what makes it the one that survives a hundred companies.

    2 and 3 cover opposite failure shapes, which is why both are here. Either
    way, this is what broken *pagination* looks like - page 1 still parses,
    pages 2..N silently stop coming - and none of it was visible before: the
    count stays comfortably above zero, so the gate passed, state was
    rewritten to the truncated set (dropping every id past page 1), and real
    new postings beyond page 1 were never detected.

    All three eventually give up and accept the new count; none of them may
    hold forever, because a gate with no exit is a gate that either loses jobs
    (the partial cases) or repeats one alert until it is ignored (the zero).
    """
    if profile.zero_is_plausible:
        return "", 0

    previous = state.get("last_count", 0)
    if count == 0 and previous > 0:
        return (f"{slug}: got 0 jobs after the previous run returned "
                f"{previous}. State was NOT updated - this is likely a broken "
                "selector, not 'no open jobs'.", TOTAL_ZERO_ACCEPT_AFTER)

    floor = profile.expected_min_jobs
    if floor > 0 and count < floor <= previous:
        return (f"{slug}: got {count} jobs, below the profile's "
                f"expected_min_jobs floor of {floor}, after the previous run "
                f"returned {previous}. State was NOT updated - a partial drop "
                "like this is what broken pagination looks like. If the "
                "company genuinely shrank, lower expected_min_jobs.",
                PARTIAL_COLLAPSE_ACCEPT_AFTER)

    if previous >= _COLLAPSE_MIN_BASELINE and count < previous * _COLLAPSE_RATIO:
        return (f"{slug}: got {count} jobs after the previous run returned "
                f"{previous} - a drop of more than "
                f"{(1 - _COLLAPSE_RATIO) * 100:g}% in one cron interval. State "
                "was NOT updated. This needs no number in the profile: it is "
                "measured against the company's own last healthy run.",
                PARTIAL_COLLAPSE_ACCEPT_AFTER)

    return "", 0


def process_company(slug: str, fetched: list[Job], profile) -> RunResult:
    """Runs the diff for a single company and updates its state - unless a
    silent-failure was suspected, in which case the state is left exactly
    as it was.

    profile is src.profiles.Profile - only the two health fields are needed
    here, .zero_is_plausible and .expected_min_jobs.
    """
    state = load_state(slug)
    now = datetime.now(timezone.utc).isoformat()
    count = len(fetched)

    # *** The health gate - the core of the anti "silent zero" mechanism ***
    # A count that collapsed against a previously healthy run, on a company
    # where that isn't plausible, is not a legitimate outcome - it's most
    # likely a broken selector. State is left untouched, no "new jobs" are
    # sent, but the failure is still reported.
    suspicion, accept_after = _collapse_suspicion(slug, count, state, profile)
    accepted = ""
    if suspicion:
        failures = state.get("consecutive_failures", 0) + 1
        if failures >= accept_after:
            # Held out long enough. The drop has now been reported on every
            # one of those runs, so it is not going unnoticed; carrying on
            # blocking is the more expensive mistake. For a partial collapse
            # it blocks every genuinely new posting at this company; for a
            # total zero it repeats one alert every three hours until the user
            # stops reading maintenance alerts entirely.
            headline = ("accepting that this company currently has no "
                        "Israel-relevant open roles"
                        if count == 0 else
                        f"accepting {count} jobs as the new normal")
            accepted = (f"{slug}: {headline}, after {failures} consecutive "
                        "runs reporting a collapse. This is the last alert "
                        "about it - new-job detection resumes from this count. "
                        "If this was a real breakage rather than a real drop, "
                        "the jobs it stopped returning are now un-seen and "
                        "will re-alert once it is fixed.")
        else:
            state["consecutive_failures"] = failures
            _write_state(slug, state)   # only the counter updates, "jobs" stays as-is
            return RunResult(slug=slug, status="empty_suspicious", new_jobs=[],
                             total_fetched=count, message=suspicion)

    # A healthy run (or a company where 0 is plausible) - do a real diff
    previous_ids = set(state.get("jobs", {}).keys())
    current_ids = {j.id for j in fetched}
    new_ids = current_ids - previous_ids
    new_jobs = [j for j in fetched if j.id in new_ids]

    # Jobs that disappeared just don't make it into the updated state - no
    # alert for that (decided: alerts are for new jobs only)
    state["jobs"] = {j.id: {"title": j.title, "first_seen":
                            state.get("jobs", {}).get(j.id, {}).get("first_seen", now)}
                     for j in fetched}
    state["last_success"] = now
    state["last_count"] = count
    state["consecutive_failures"] = 0
    _write_state(slug, state)

    # `message` on an "ok" result means "this run was healthy, but something
    # about it is worth telling the user" - currently only an accepted
    # collapse. run.py sends it as a maintenance note and then carries on
    # with the new jobs as normal.
    return RunResult(slug=slug, status="ok", new_jobs=new_jobs,
                     total_fetched=count, message=accepted)


def seed_company(slug: str, fetched: list[Job]) -> None:
    """Initial seeding for a company: writes full state without going
    through the diff/health-gate, and without returning any "new" jobs.
    Used for the manual --seed run (decided: never automatic) and for the
    /add flow when a new company is added."""
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "last_success": now, "last_count": len(fetched),
        "consecutive_failures": 0,
        "jobs": {j.id: {"title": j.title, "first_seen": now} for j in fetched},
    }
    _write_state(slug, state)


def restore_state(slug: str, previous: dict) -> None:
    """Puts a company's state file back to a snapshot taken before this run
    touched it.

    Used for exactly one thing: an alert that could not be sent. State is
    written BEFORE notification (deliberately - see filters.py), so a failed
    send leaves jobs recorded as "seen" that the user never saw. Rewinding
    just that company un-sees them, so the next run re-detects and re-sends
    them, while every other company's work still commits normally. See the
    note in run.py on what this replaced."""
    _write_state(slug, previous)


def record_failure(slug: str) -> int:
    """Bumps the consecutive-failure counter without touching `jobs`.
    Returns the new count.

    This exists because a *raised* fetch error and a suspicious zero are the
    same event as far as the maintenance alert is concerned, but only the
    latter used to be counted - process_company's health gate incremented,
    while an exception out of the fetcher went straight past it to
    should_alert_failure, which therefore read a counter nothing had ever
    raised and stayed False forever.

    That gap was mostly theoretical while a slow browser page decayed into an
    empty list (the health gate caught it). It stopped being theoretical the
    moment browser.py started raising ListingNeverRendered for exactly that
    case: without this function, the more accurate error would have alerted
    LESS than the silent zero it replaced."""
    state = load_state(slug)
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    _write_state(slug, state)
    return state["consecutive_failures"]


def should_alert_failure(slug: str) -> bool:
    """Whether consecutive failures have crossed the threshold for a
    maintenance alert on Telegram."""
    state = load_state(slug)
    return state.get("consecutive_failures", 0) >= FAILURE_ALERT_THRESHOLD


# ---------------------------------------------------------------------------
# The implausible-volume gate - the run-level twin of the health gate above
# ---------------------------------------------------------------------------
#
# The per-company gate catches a company that went QUIET. This catches the
# opposite failure, which only exists at scale: a run in which a great many
# companies simultaneously report a great many new jobs.
#
# That shape is almost never a hiring spree. It is what losing or resetting
# state looks like - every currently-open posting reads as new at once, so at
# 141 companies the bot would deliver something like 1,350 postings across 141
# messages. The per-company health gate cannot see this at all: it only ever
# asks whether a count COLLAPSED, and "everything is new" collapses nothing.
#
# What makes this worth its own mechanism rather than a cap is where the jobs
# go. state is written BEFORE notification, so simply declining to send would
# mark all 1,350 as seen and they would never be alerted again - the exact
# silent loss this project exists to prevent. So a suppressed flood REWINDS the
# companies it suppressed (see run.py), leaving those jobs un-seen and
# re-detected next run.

# What counts as implausible, measured against reality rather than guessed: a
# full live run across all 141 imported companies returns ~1,350 Israel-relevant
# postings in total. A three-hour window producing more than 50 genuinely NEW
# ones - about 4% of the entire corpus turning over between two runs - is not
# something any board in this project has been observed to do.
DEFAULT_IMPLAUSIBLE_NEW_JOBS = 50

# *** Why that number cannot stay absolute. ***
#
# 50 was never really "50". It was "~4% of the corpus", measured when the
# corpus was 1,350 postings across 141 companies. The RATIO is the finding; the
# integer is an artefact of the corpus size on the day it was taken.
#
# Left absolute, the gate tightens by itself every time a company is added,
# because the corpus grows while the limit does not. At ~2,100 postings (256
# companies) 50 is already ~2.4%; at the ~15,000 postings a 2,000-company
# corpus would carry it is ~0.3%, and ordinary churn would trip the gate on
# most runs - holding every alert and rewinding companies continuously, which
# is precisely the "warns and never delivers" failure FLOOD_ACCEPT_AFTER exists
# to prevent. The escape hatch would fire constantly and the gate would become
# noise the user learns to ignore.
#
# So the threshold is expressed as the fraction it always was, against the
# corpus actually seen this run:
IMPLAUSIBLE_NEW_JOBS_FRACTION = 50 / 1350        # ~3.7%, the measured basis

# ...with a floor, so the gate can only ever LOOSEN as the corpus grows, never
# tighten below the one value that was measured against reality. A small corpus
# keeps exactly today's behaviour; scaling down would be a change nothing
# justifies.
MIN_IMPLAUSIBLE_NEW_JOBS = DEFAULT_IMPLAUSIBLE_NEW_JOBS

# Worth stating plainly, because it is the reason this is safe: loosening does
# not blunt the failure the gate was built for. Losing state makes EVERY open
# posting look new at once, so a full reset reports ~100% of the corpus against
# a threshold of ~3.7% of it - caught by a factor of 27 whatever the size. The
# gate still catches any reset touching more than about a twenty-fifth of the
# corpus. What it stops doing is mistaking a bigger corpus for a bigger problem.

# ...and the escape hatch, for exactly the same reason PARTIAL_COLLAPSE_ACCEPT_
# AFTER exists. Suppressing is not free: while suppressed, nothing is
# delivered. If the volume is real - a seed gap closing, a genuine hiring
# surge, a threshold set too low - holding forever would mean the bot warns
# and never delivers, which is a worse failure than the flood. Two consecutive
# runs (~6 hours) report the volume twice, crossing the user's attention
# threshold, and then it gives up and sends.
FLOOD_ACCEPT_AFTER = 2

RUN_STATE_PATH = STATE_DIR.parent / "run.json"


def implausible_new_jobs_threshold(corpus_postings: int | None = None) -> int:
    """The limit for this run, scaled to the corpus it is judging.

    `corpus_postings` is how many postings the run actually saw across every
    company - not how many are new. Passing None keeps the historical absolute
    default, which is what makes this safe to call from anywhere: a caller that
    has no corpus figure gets exactly the old behaviour rather than a wrong
    proportional one.

    JOB_ALERT_MAX_NEW_JOBS still wins outright when set. It is an absolute
    override on purpose - it exists to pin the threshold during an
    investigation, and a knob that silently rescaled itself would be useless
    for that. A bad value falls back rather than raising, the same rule the
    worker counts and the fetch budget follow."""
    import os
    raw = os.environ.get("JOB_ALERT_MAX_NEW_JOBS")
    if raw is not None:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_IMPLAUSIBLE_NEW_JOBS

    if not corpus_postings or corpus_postings < 0:
        return DEFAULT_IMPLAUSIBLE_NEW_JOBS

    scaled = round(corpus_postings * IMPLAUSIBLE_NEW_JOBS_FRACTION)
    return max(MIN_IMPLAUSIBLE_NEW_JOBS, scaled)


def load_run_state() -> dict:
    """Run-level state. An unreadable or absent file reads as zeros.

    Deliberately NOT raising the way load_state does: this file only carries a
    counter for the flood gate, and failing a run over it would let a
    cosmetic problem block every alert - the opposite of what the gate is
    for."""
    if not RUN_STATE_PATH.exists():
        return {"flood_suppressed_runs": 0, "last_suppressed_at": None}
    try:
        stored = json.loads(RUN_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"flood_suppressed_runs": 0, "last_suppressed_at": None}
    if not isinstance(stored, dict):
        return {"flood_suppressed_runs": 0, "last_suppressed_at": None}
    stored.setdefault("flood_suppressed_runs", 0)
    stored.setdefault("last_suppressed_at", None)
    return stored


def _write_run_state(state: dict) -> None:
    RUN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUN_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(RUN_STATE_PATH)


def record_flood_suppressed() -> int:
    """Counts one suppressed run and returns the new consecutive count."""
    state = load_run_state()
    state["flood_suppressed_runs"] = state.get("flood_suppressed_runs", 0) + 1
    state["last_suppressed_at"] = datetime.now(timezone.utc).isoformat()
    _write_run_state(state)
    return state["flood_suppressed_runs"]


def clear_flood_counter() -> None:
    """Resets the counter after a run that sent normally. Only writes when
    there is something to reset, so an ordinary run doesn't touch the file and
    doesn't put a line in the state commit."""
    state = load_run_state()
    if not state.get("flood_suppressed_runs"):
        return
    state["flood_suppressed_runs"] = 0
    _write_run_state(state)


def flood_decision(new_job_count: int,
                   corpus_postings: int | None = None) -> tuple[bool, str]:
    """(suppress, message) for a run about to send `new_job_count` jobs.

    `corpus_postings` scales the threshold to the run's own corpus - see
    implausible_new_jobs_threshold. Omitting it keeps the absolute default.

    Returns suppress=False and an empty message for any ordinary run, so the
    common path costs one comparison and no file read beyond the counter."""
    threshold = implausible_new_jobs_threshold(corpus_postings)
    if new_job_count <= threshold:
        return False, ""

    suppressed_so_far = load_run_state().get("flood_suppressed_runs", 0)
    if suppressed_so_far >= FLOOD_ACCEPT_AFTER:
        return False, (
            f"{new_job_count} new jobs - still above the plausible limit of "
            f"{threshold}, but this is run {suppressed_so_far + 1} in a row "
            "reporting it, so they are being sent rather than held any longer. "
            "If this was a state problem rather than real hiring, expect a "
            "large batch now and normal volume afterwards.")

    return True, (
        f"{new_job_count} new jobs in a single run, which is above the "
        f"plausible limit of {threshold}. At this volume it is usually lost or "
        "reset state making every open posting look new, not real hiring - so "
        "the alerts were NOT sent.\n\n"
        "The jobs are NOT lost: the affected companies were rewound, so they "
        "are un-seen and will be re-detected next run. If the volume is "
        f"genuine, it will be sent automatically after {FLOOD_ACCEPT_AFTER} "
        "consecutive runs report it.")
