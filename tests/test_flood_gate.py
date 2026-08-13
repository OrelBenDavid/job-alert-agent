# -*- coding: utf-8 -*-
"""
The implausible-volume gate: the run-level twin of the per-company health gate.

The per-company gate catches a company that went quiet. This catches the
opposite failure, which only exists at scale - lost or reset state making every
open posting look new at once, which at 141 companies is ~1,350 postings across
141 messages.

The property that matters most here is NOT that a flood is withheld. It is that
withholding never loses a job: state is written before notification, so a
withheld batch is already recorded as seen, and the gate must rewind it. A gate
that suppressed without rewinding would trade a noisy flood for a permanent
silent loss - strictly worse than the flood it prevents.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import state as state_mod
import run as run_mod
from models import Job


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Redirects both state paths at the module level so nothing here can
    touch the repo's real state/."""
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path / "seen")
    monkeypatch.setattr(state_mod, "RUN_STATE_PATH", tmp_path / "run.json")
    (tmp_path / "seen").mkdir()
    return tmp_path


def _jobs(n, company="acme"):
    return [Job(id=f"{company}-{i}", title=f"Job {i}", location="Tel Aviv",
                url=f"https://x/{company}/{i}", company=company)
            for i in range(n)]


class _FakeProfile:
    def __init__(self, slug):
        self.slug = slug
        self.name = slug.title()


# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------

def test_an_ordinary_volume_is_not_suppressed(isolated_state):
    suppress, message = state_mod.flood_decision(10)
    assert suppress is False
    assert message == ""


def test_a_volume_exactly_at_the_threshold_is_not_suppressed(isolated_state):
    """The threshold is a limit to exceed, not to reach - otherwise the
    documented number is off by one from the behaviour."""
    suppress, _ = state_mod.flood_decision(
        state_mod.DEFAULT_IMPLAUSIBLE_NEW_JOBS)
    assert suppress is False


def test_a_volume_above_the_threshold_is_suppressed(isolated_state):
    suppress, message = state_mod.flood_decision(
        state_mod.DEFAULT_IMPLAUSIBLE_NEW_JOBS + 1)
    assert suppress is True
    assert "NOT lost" in message      # the user must be told this explicitly


def test_the_threshold_is_env_overridable(isolated_state, monkeypatch):
    monkeypatch.setenv("JOB_ALERT_MAX_NEW_JOBS", "5")
    assert state_mod.implausible_new_jobs_threshold() == 5
    assert state_mod.flood_decision(6)[0] is True


def test_a_bad_env_value_falls_back_instead_of_raising(isolated_state,
                                                       monkeypatch):
    """A typo in a workflow env var must not be able to take a run down."""
    monkeypatch.setenv("JOB_ALERT_MAX_NEW_JOBS", "not-a-number")
    assert (state_mod.implausible_new_jobs_threshold()
            == state_mod.DEFAULT_IMPLAUSIBLE_NEW_JOBS)


# ---------------------------------------------------------------------------
# The escape hatch
# ---------------------------------------------------------------------------

def test_it_gives_up_and_sends_after_enough_consecutive_runs(isolated_state):
    """Suppressing forever is a worse failure than the flood: the bot would
    warn and never deliver. Mirrors PARTIAL_COLLAPSE_ACCEPT_AFTER."""
    big = state_mod.DEFAULT_IMPLAUSIBLE_NEW_JOBS + 1
    for _ in range(state_mod.FLOOD_ACCEPT_AFTER):
        assert state_mod.flood_decision(big)[0] is True
        state_mod.record_flood_suppressed()

    suppress, message = state_mod.flood_decision(big)
    assert suppress is False
    assert "sent rather than held" in message


def test_a_normal_run_clears_the_counter(isolated_state):
    state_mod.record_flood_suppressed()
    assert state_mod.load_run_state()["flood_suppressed_runs"] == 1
    state_mod.clear_flood_counter()
    assert state_mod.load_run_state()["flood_suppressed_runs"] == 0


def test_an_unreadable_run_state_reads_as_zero_rather_than_raising(
        isolated_state):
    """This file only carries a counter. Failing a run over it would let a
    cosmetic problem block every alert - the opposite of the gate's purpose."""
    state_mod.RUN_STATE_PATH.write_text("{ this is not json", encoding="utf-8")
    assert state_mod.load_run_state()["flood_suppressed_runs"] == 0


# ---------------------------------------------------------------------------
# Delivery - the part that must not lose jobs
# ---------------------------------------------------------------------------

def test_a_suppressed_flood_rewinds_every_company_it_withheld(isolated_state):
    """The single most important assertion in this file. Without the rewind,
    withholding marks ~1,350 jobs as seen that the user never saw, and they are
    never alerted again."""
    pending = [(_FakeProfile("acme"), _jobs(40, "acme"), {}),
               (_FakeProfile("beta"), _jobs(40, "beta"), {})]
    previous = {"acme": {"jobs": {"old": {}}, "last_success": "t", "last_count": 1,
                         "consecutive_failures": 0},
                "beta": {"jobs": {"old": {}}, "last_success": "t", "last_count": 1,
                         "consecutive_failures": 0}}
    # Pre-write the "already seen" state the run would have left behind.
    for slug in ("acme", "beta"):
        state_mod.seed_company(slug, _jobs(40, slug))
        assert len(state_mod.load_state(slug)["jobs"]) == 40

    send_failures = []
    with patch.object(run_mod, "notify_new_jobs") as send, \
         patch.object(run_mod, "notify_maintenance") as warn:
        run_mod._deliver(pending, previous, send_failures)

    assert send.call_count == 0            # nothing delivered
    assert warn.call_count == 1            # one warning instead
    for slug in ("acme", "beta"):
        # Rewound to the pre-run snapshot, so the 80 jobs are un-seen and will
        # be re-detected next run.
        assert state_mod.load_state(slug)["jobs"] == {"old": {}}
    assert send_failures == []


def test_the_warning_names_the_biggest_contributors(isolated_state):
    pending = [(_FakeProfile("small"), _jobs(5, "small"), {}),
               (_FakeProfile("huge"), _jobs(60, "huge"), {})]
    previous = {"small": {"jobs": {}, "last_success": "t", "last_count": 0,
                          "consecutive_failures": 0},
                "huge": {"jobs": {}, "last_success": "t", "last_count": 0,
                         "consecutive_failures": 0}}
    with patch.object(run_mod, "notify_new_jobs"), \
         patch.object(run_mod, "notify_maintenance") as warn:
        run_mod._deliver(pending, previous, [])

    body = warn.call_args[0][1]
    assert "Huge (60)" in body


def test_an_ordinary_run_delivers_every_company(isolated_state):
    pending = [(_FakeProfile("acme"), _jobs(3, "acme"), {}),
               (_FakeProfile("beta"), _jobs(2, "beta"), {})]
    with patch.object(run_mod, "notify_new_jobs") as send, \
         patch.object(run_mod, "notify_maintenance") as warn:
        run_mod._deliver(pending, {}, [])

    assert send.call_count == 2
    assert warn.call_count == 0


def test_a_run_with_nothing_to_send_sends_nothing(isolated_state):
    with patch.object(run_mod, "notify_new_jobs") as send, \
         patch.object(run_mod, "notify_maintenance") as warn:
        run_mod._deliver([], {}, [])
    assert send.call_count == 0 and warn.call_count == 0


def test_a_failed_send_still_rewinds_only_that_company(isolated_state):
    """The pre-existing per-company guarantee must survive the restructure:
    one company's failed send must not re-send every other company's jobs."""
    pending = [(_FakeProfile("acme"), _jobs(2, "acme"), {}),
               (_FakeProfile("beta"), _jobs(2, "beta"), {})]
    previous = {"acme": {"jobs": {"old": {}}, "last_success": "t",
                         "last_count": 1, "consecutive_failures": 0},
                "beta": {"jobs": {"old": {}}, "last_success": "t",
                         "last_count": 1, "consecutive_failures": 0}}
    for slug in ("acme", "beta"):
        state_mod.seed_company(slug, _jobs(2, slug))

    def fail_for_acme(name, jobs, tags=None):
        if name == "Acme":
            raise RuntimeError("telegram down")

    send_failures = []
    with patch.object(run_mod, "notify_new_jobs", side_effect=fail_for_acme), \
         patch.object(run_mod, "notify_maintenance"):
        run_mod._deliver(pending, previous, send_failures)

    assert state_mod.load_state("acme")["jobs"] == {"old": {}}   # rewound
    assert len(state_mod.load_state("beta")["jobs"]) == 2        # untouched


def test_a_flood_whose_rewind_fails_makes_the_run_fatal(isolated_state):
    """If the jobs cannot be un-seen, the only way to stop them being lost is
    to commit nothing - the same fallback a failed send already uses."""
    pending = [(_FakeProfile("acme"), _jobs(60, "acme"), {})]
    send_failures = []
    with patch.object(run_mod, "notify_new_jobs"), \
         patch.object(run_mod, "notify_maintenance"):
        # No pre-run snapshot for acme -> the rewind cannot run.
        run_mod._deliver(pending, {}, send_failures)

    assert send_failures, "a failed rewind must reach the non-zero exit path"


def test_a_failing_warning_does_not_take_the_run_down(isolated_state):
    """The warning is best-effort; the rewind is what matters."""
    pending = [(_FakeProfile("acme"), _jobs(60, "acme"), {})]
    previous = {"acme": {"jobs": {}, "last_success": "t", "last_count": 0,
                         "consecutive_failures": 0}}
    with patch.object(run_mod, "notify_new_jobs"), \
         patch.object(run_mod, "notify_maintenance",
                      side_effect=RuntimeError("telegram down")):
        run_mod._deliver(pending, previous, [])   # must not raise
