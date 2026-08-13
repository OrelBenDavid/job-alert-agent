# -*- coding: utf-8 -*-
"""
State management per company: which job ids have already been seen, how
many jobs were successfully fetched last time, and how many consecutive
failures the company has accumulated.

This is where the project's most important principle is enforced: if a run
returns 0 jobs after a previous run returned a healthy count, that's a
failure, not "no jobs". State is not overwritten, and the alert that goes
out is a maintenance alert, not "0 new jobs".
"""

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

# How many consecutive PARTIAL collapses to hold out for before accepting the
# lower count as the new normal.
#
# This escape hatch is not optional, and it is the difference between the two
# kinds of gate hit. Freezing state on a total zero is free - there are no
# jobs to miss. Freezing it on a partial collapse is NOT: process_company
# returns no new jobs while frozen, so a false positive silently stops
# detecting real postings at that company, which is precisely the "quiet while
# looking healthy" failure the gate exists to prevent. Holding for three runs
# (~9 hours) reports the drop twice - crossing the alert threshold - and then
# resumes on its own rather than waiting for a human who may be asleep.
PARTIAL_COLLAPSE_ACCEPT_AFTER = 3


def _collapse_suspicion(slug: str, count: int, state: dict,
                        profile) -> tuple[str, bool]:
    """The health gate's decision: (message, is_partial). "" means healthy.

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

    `is_partial` marks the two that must eventually give up and accept the new
    count - see PARTIAL_COLLAPSE_ACCEPT_AFTER.
    """
    if profile.zero_is_plausible:
        return "", False

    previous = state.get("last_count", 0)
    if count == 0 and previous > 0:
        return (f"{slug}: got 0 jobs after the previous run returned "
                f"{previous}. State was NOT updated - this is likely a broken "
                "selector, not 'no open jobs'.", False)

    floor = profile.expected_min_jobs
    if floor > 0 and count < floor <= previous:
        return (f"{slug}: got {count} jobs, below the profile's "
                f"expected_min_jobs floor of {floor}, after the previous run "
                f"returned {previous}. State was NOT updated - a partial drop "
                "like this is what broken pagination looks like. If the "
                "company genuinely shrank, lower expected_min_jobs.", True)

    if previous >= _COLLAPSE_MIN_BASELINE and count < previous * _COLLAPSE_RATIO:
        return (f"{slug}: got {count} jobs after the previous run returned "
                f"{previous} - a drop of more than "
                f"{(1 - _COLLAPSE_RATIO) * 100:g}% in one cron interval. State "
                "was NOT updated. This needs no number in the profile: it is "
                "measured against the company's own last healthy run.", True)

    return "", False


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
    suspicion, is_partial = _collapse_suspicion(slug, count, state, profile)
    accepted = ""
    if suspicion:
        failures = state.get("consecutive_failures", 0) + 1
        if is_partial and failures >= PARTIAL_COLLAPSE_ACCEPT_AFTER:
            # Held out long enough. The drop has now been reported on every
            # one of those runs, so it is not going unnoticed; carrying on
            # blocking is the more expensive mistake, because it also blocks
            # every genuinely new posting at this company.
            accepted = (f"{slug}: accepting {count} jobs as the new normal "
                        f"after {failures} consecutive runs reporting a "
                        "collapse. New-job detection resumes from this count. "
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
