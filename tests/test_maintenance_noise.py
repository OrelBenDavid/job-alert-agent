# -*- coding: utf-8 -*-
"""
The maintenance channel's signal-to-noise, which is a correctness property
and not a matter of taste.

*** What was measured, 2026-08-23 ***

Over the five days to that date the bot sent roughly 26 maintenance messages
against 5-10 job alerts on a working day. Every one of the 26 came from four
companies - panaya, pontera, speak, johnson_johnson - and not one of them was
broken. Each had a single Israel-relevant posting, it was filled, and two
independent mechanisms turned that into a stream:

  the EVENT   `1 -> 0` counted as a collapse, because the zero branch of the
              health gate had no minimum baseline while the ratio branch did.

  the REPEAT  once a company was two runs into trouble, the identical alert
              went out on every run after that - eight times a day, bounded
              only by the accept-after hatch.

Both are fixed here, and they are independent: the baseline removes events
that were never real, the doubling schedule bounds the repeats of the ones
that are. A third change batches whatever survives into one message per run.

The cost of a maintenance alert is not the notification. It is that a channel
which cries wolf every three hours is one the user stops reading, and the next
alert will be real.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import notifier
import state as state_mod
from models import Job


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path)
    yield tmp_path


def _job(i):
    return Job(id="id%d" % i, title="Backend Engineer", location="Tel Aviv",
               url="https://x.com/%d" % i, company="acme")


def _profile(zero_is_plausible=False, expected_min_jobs=0):
    return SimpleNamespace(zero_is_plausible=zero_is_plausible,
                           expected_min_jobs=expected_min_jobs)


# ---------------------------------------------------------------------------
# The event: a small board emptying out is not a collapse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seeded", [1, 2])
def test_a_small_board_going_to_zero_is_ordinary_churn(seeded):
    """The panaya case, and the single largest source of maintenance noise in
    the project's history. One open role, it gets filled, the count goes to
    zero - and there is no number a human could have set that would have made
    the old gate stop reporting it."""
    state_mod.seed_company("acme", [_job(i) for i in range(seeded)])

    result = state_mod.process_company("acme", [], _profile())

    assert result.status == "ok"
    assert result.message == ""
    saved = state_mod.load_state("acme")
    assert saved["jobs"] == {}          # state moves on, rather than freezing
    assert saved["last_count"] == 0
    assert saved["consecutive_failures"] == 0
    assert state_mod.should_alert_failure("acme") is False


def test_the_baseline_is_where_the_gate_starts_caring():
    """The boundary itself, so a future edit to the constant has to come here
    and say what it is trading."""
    state_mod.seed_company("acme",
                           [_job(i) for i in range(state_mod.ZERO_COLLAPSE_MIN_BASELINE)])

    result = state_mod.process_company("acme", [], _profile())

    assert result.status == "empty_suspicious"
    assert "likely a broken selector" in result.message
    # Held, exactly as before: state is NOT overwritten.
    assert len(state_mod.load_state("acme")["jobs"]) == \
        state_mod.ZERO_COLLAPSE_MIN_BASELINE


def test_a_small_board_zero_is_not_re_routed_through_expected_min_jobs():
    """The trap this fix could have walked into.

    `expected_min_jobs=1` with a previous count of 1 satisfies the partial
    branch's `0 < 1 <= 1` exactly. Without the `count` guard there, exempting
    the small board from the zero branch would have sent it straight into the
    floor branch instead - the same alert, a different sentence, and the fix
    would have looked like it worked while changing nothing."""
    state_mod.seed_company("acme", [_job(1)])

    result = state_mod.process_company("acme", [],
                                       _profile(expected_min_jobs=1))

    assert result.status == "ok"
    assert result.message == ""


def test_a_real_collapse_at_a_real_board_is_untouched():
    """The gate still has to do its job. This is the shape it exists for -
    a board with a genuine set of postings returning nothing at all."""
    state_mod.seed_company("acme", [_job(i) for i in range(40)])

    result = state_mod.process_company("acme", [], _profile())

    assert result.status == "empty_suspicious"
    assert len(state_mod.load_state("acme")["jobs"]) == 40


# ---------------------------------------------------------------------------
# The repeat: alert when it breaks, then only when the silence doubles
# ---------------------------------------------------------------------------

def test_the_crossing_itself_is_always_loud():
    assert state_mod.failure_alert_due(state_mod.FAILURE_ALERT_THRESHOLD) is True


def test_below_the_threshold_says_nothing():
    for failures in range(state_mod.FAILURE_ALERT_THRESHOLD):
        assert state_mod.failure_alert_due(failures) is False


def test_a_company_that_stays_broken_is_reported_on_a_doubling_schedule():
    """Not silence - a dead endpoint must keep saying so - but a bounded
    amount of it. Each gap is twice the last, so the alert survives for weeks
    without ever being the reason the channel stops being read."""
    due = [n for n in range(1, 65) if state_mod.failure_alert_due(n)]
    assert due == [2, 4, 8, 16, 32, 64]


def test_a_week_of_a_dead_endpoint_costs_five_messages_not_fifty_five():
    """The number that made this worth changing. The cron is every 3 hours, so
    a week of consecutive failures is 56 runs - and the old `>=` rule alerted
    on 55 of them."""
    runs_in_a_week = 7 * 8
    assert sum(1 for n in range(1, runs_in_a_week + 1)
               if state_mod.failure_alert_due(n)) == 5


def test_the_counter_resetting_makes_the_next_break_loud_again():
    """The doubling must not make a recovered-then-rebroken company quiet:
    a healthy run zeroes the counter, so the next failure starts over at the
    crossing."""
    state_mod.seed_company("acme", [_job(i) for i in range(10)])
    for _ in range(3):
        state_mod.record_failure("acme")
    assert state_mod.should_alert_failure("acme") is False   # 3 - mid-gap

    state_mod.process_company("acme", [_job(i) for i in range(10)], _profile())
    assert state_mod.load_state("acme")["consecutive_failures"] == 0

    state_mod.record_failure("acme")
    state_mod.record_failure("acme")
    assert state_mod.should_alert_failure("acme") is True


# ---------------------------------------------------------------------------
# The batch: one message per run, not one per company
# ---------------------------------------------------------------------------

def test_no_events_sends_nothing():
    assert notifier.format_maintenance_digest([]) == []


def test_a_single_event_still_renders_as_the_plain_alert():
    """The common case must look exactly as it always has - a digest header
    over one company would be noise about the noise."""
    messages = notifier.format_maintenance_digest([("panaya", "Fetch error: 500")])
    assert messages == [notifier.format_maintenance_alert("panaya",
                                                          "Fetch error: 500")]


def test_several_events_become_one_message_that_states_the_count():
    """The count is the thing a per-company message cannot say, and it is the
    whole diagnosis: three companies is churn, forty is the bot itself."""
    events = [("panaya", "Fetch error: 500"),
              ("pontera", "Fetch error: timeout"),
              ("speak", "Fetch error: 404")]

    messages = notifier.format_maintenance_digest(events)

    assert len(messages) == 1
    assert "3 companies" in messages[0]
    for slug, _ in events:
        assert slug in messages[0]


def test_a_digest_too_big_for_telegram_splits_and_loses_nothing():
    """Same rule as a new-jobs batch: an oversized maintenance digest must
    span messages rather than be truncated or rejected. A 400 from Telegram
    costs every event in the batch, which at that size is exactly when the
    events matter most."""
    events = [(f"company{i}", "Fetch error: " + "x" * 200) for i in range(60)]

    messages = notifier.format_maintenance_digest(events)

    assert len(messages) > 1
    assert all(len(m) <= notifier.TELEGRAM_MAX_CHARS for m in messages)
    joined = "\n".join(messages)
    for slug, _ in events:
        assert slug in joined


def test_the_digest_escapes_markdown_in_both_halves():
    """A slug or an error message carrying an unescaped `.` or `-` is a 400 on
    the whole digest - the same failure escape_mdv2 exists for everywhere
    else."""
    messages = notifier.format_maintenance_digest(
        [("acme_co", "got 0 jobs (was 4.5)"), ("beta-co", "HTTP 500 - dead")])

    assert "\\(" in messages[0] and "\\." in messages[0]
    assert "beta\\-co" in messages[0]


# ---------------------------------------------------------------------------
# ...end to end: one run, one message
# ---------------------------------------------------------------------------

def _write_profile(profiles_dir, slug):
    import json
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / f"{slug}.json").write_text(json.dumps({
        "schema_version": 3, "slug": slug, "name": slug.title(),
        "enabled": True, "careers_url": f"https://jobs.lever.co/{slug}",
        "fetch_type": "api",
        "israel_filter": {"method": "post_fetch"},
        "api": {"platform": "lever",
                "endpoint": f"https://api.lever.co/v0/postings/{slug}",
                "fields": {"id": "id", "title": "text",
                           "location": "categories.location",
                           "url": "hostedUrl"}},
        "health": {"expected_min_jobs": 0},
        "verified_on": "2026-08-12",
    }), encoding="utf-8")


def test_a_run_with_several_broken_companies_sends_one_message(monkeypatch,
                                                               tmp_path):
    """The batching rule, at the level it actually matters. Before this, one
    transient network problem across ten companies was ten ⚠️ messages
    interleaved with the job alerts - and there was no way to tell that from
    ten separate faults."""
    import profiles as profiles_mod
    import run as run_mod
    from fetchers import FetchOutcome

    profiles_dir = tmp_path / "profiles"
    for slug in ("acme", "beta", "gamma"):
        _write_profile(profiles_dir, slug)
        # Seeded above the zero baseline so an empty fetch is a real collapse.
        state_mod.seed_company(slug, [_job(i) for i in range(10)])

    monkeypatch.setattr(run_mod, "load_enabled",
                        lambda: profiles_mod.load_enabled(profiles_dir))
    monkeypatch.setattr(run_mod, "process_commands", lambda: None)
    monkeypatch.setattr(run_mod, "fetch_all",
                        lambda profiles, deadline=None: {
                            p.slug: FetchOutcome(p.slug, None,
                                                 RuntimeError("HTTP 503"), 0.0)
                            for p in profiles})

    digests = []
    monkeypatch.setattr(run_mod, "notify_maintenance_digest", digests.append)
    monkeypatch.setattr(run_mod, "notify_maintenance",
                        lambda slug, msg: pytest.fail(
                            "a per-company alert escaped the digest"))

    run_mod._run_normal()       # failure 1 of 3 - below the threshold
    assert digests == []

    run_mod._run_normal()       # failure 2 - all three cross together
    assert len(digests) == 1
    assert sorted(slug for slug, _ in digests[0]) == ["acme", "beta", "gamma"]

    run_mod._run_normal()       # failure 3 - not a doubling, so silence
    assert len(digests) == 1

    run_mod._run_normal()       # failure 4 - doubled, so it speaks again
    assert len(digests) == 2
