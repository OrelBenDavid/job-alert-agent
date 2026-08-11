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


def _state_path(slug: str) -> Path:
    return STATE_DIR / f"{slug}.json"


def load_state(slug: str) -> dict:
    """Loads a company's existing state. A brand-new company (never seeded)
    gets empty state back - that's expected, and run.py treats it as a case
    that needs a manual seed, not a normal run."""
    path = _state_path(slug)
    if not path.exists():
        return {"last_success": None, "last_count": 0,
                "consecutive_failures": 0, "jobs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(slug: str, state: dict) -> None:
    """Atomic write: write to a temp file, then replace - so a crash mid-
    write (e.g. the runner gets killed on timeout) never leaves a half-
    written state file."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(slug)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def process_company(slug: str, fetched: list[Job], profile) -> RunResult:
    """Runs the diff for a single company and updates its state - unless a
    silent-failure was suspected, in which case the state is left exactly
    as it was.

    profile is src.profiles.Profile - only .zero_is_plausible is needed here.
    """
    state = load_state(slug)
    now = datetime.now(timezone.utc).isoformat()
    count = len(fetched)

    # *** The health gate - the core of the anti "silent zero" mechanism ***
    # 0 jobs after a healthy count in the past, on a company where 0 isn't
    # considered plausible, is not a legitimate outcome - it's most likely
    # a broken selector. State is left untouched, no "new jobs" are sent
    # (there are 0...), but the failure is still reported.
    if count == 0 and state["last_count"] > 0 and not profile.zero_is_plausible:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        _write_state(slug, state)   # only the failure counter updates, "jobs" stays as-is
        status = "empty_suspicious"
        msg = (f"{slug}: got 0 jobs after the previous run returned "
               f"{state['last_count']}. State was NOT updated - this is "
               "likely a broken selector, not 'no open jobs'.")
        return RunResult(slug=slug, status=status, new_jobs=[],
                         total_fetched=0, message=msg)

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

    return RunResult(slug=slug, status="ok", new_jobs=new_jobs, total_fetched=count)


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


def should_alert_failure(slug: str) -> bool:
    """Whether consecutive failures have crossed the threshold for a
    maintenance alert on Telegram."""
    state = load_state(slug)
    return state.get("consecutive_failures", 0) >= FAILURE_ALERT_THRESHOLD
