# -*- coding: utf-8 -*-
"""
Main entry point. Two ways to run it:

    python run.py            normal run: Telegram commands -> job check -> alerts
    python run.py --seed     manual seed: fetches everything, writes state,
                             sends nothing

--seed exists for two situations: (a) the initial repo, one run right after
deployment, before the cron is turned on. (b) adding a new company - so it
doesn't blast a message with all of its existing jobs the moment it's added.
It was explicitly decided NOT to auto-seed when "no state" is detected - so
that a state file deleted by accident surfaces as a reported seed gap instead
of being silently swallowed as "a new company". A state file that exists but
can't be parsed is a third case and is reported separately: re-seeding THAT
one would mark every currently-open posting as already known.

Both modes share the fetch budget below, so a run that can't finish inside the
workflow's timeout commits the companies it did reach rather than losing all
of them. That matters most on a seed of many companies at once, which is
exactly when the whole set is least likely to fit in one run.
"""

import os
import sys
import time

from profiles import load_enabled
from fetchers import fetch_all
from state import (process_company, seed_company, should_alert_failure,
                   load_state, record_failure, restore_state, StateUnreadable)
from notifier import notify_new_jobs, notify_maintenance
from commands import process_commands
from detail import DetailBudget
from filters import build_chain, run_chain
from stats import RunStats, save_stats

# How long the fetch phase may run before it stops starting new companies.
#
# Sized against check.yml's `timeout-minutes: 20`, not against any property of
# the code. The gap between the two is what pays for everything after the
# fetch (the filter chain's detail requests and the Telegram sends) plus the
# workflow's own state commit. A run that overruns the workflow timeout is
# killed before the commit step, so ALL of its state writes are discarded and
# the next run walks into the same wall - which is why finishing early with
# some companies skipped is the better failure.
DEFAULT_FETCH_BUDGET_SECONDS = 780   # 13 min of a 20 min job


def _fetch_budget_seconds() -> int:
    """Env-overridable, and a bad value falls back rather than raising - the
    same rule the worker counts follow."""
    try:
        return max(1, int(os.environ.get("JOB_ALERT_FETCH_BUDGET_SECONDS",
                                         DEFAULT_FETCH_BUDGET_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_FETCH_BUDGET_SECONDS


def _needs_seed(slug: str) -> bool:
    """Whether this company has no usable state yet.

    An unreadable file counts as "no" - deliberately. Re-seeding a company
    whose ids are on disk but momentarily unparseable would mark every
    currently-open posting as already known, and those postings would then
    never be alerted. Restoring the file from git history is the fix; this
    command is not."""
    try:
        return load_state(slug).get("last_success") is None
    except StateUnreadable as e:
        print(f"[seed skip] {e} - restore it from git history rather than "
              "re-seeding, which would swallow every currently-open posting",
              file=sys.stderr)
        return False


def _run_seed(force: bool = False) -> None:
    """Seeds companies that have no state yet.

    *** Why it skips the ones already seeded ***

    Seeding an already-seeded company is not harmful but it is not free
    either: it resets every `first_seen` to the seed timestamp, losing the
    only record of when a posting was first observed. That made the command
    unusable for its most common case - dropping several new profiles in at
    once and seeding just those - and the workaround was to seed each new
    company by hand (see the wix note in the README).

    Skipping them also means the already-seeded companies cost no fetch at
    all, so adding twenty profiles to a hundred existing ones is a twenty-
    company run, not a hundred-and-twenty-company one.

    `--force` re-seeds everything, for the rare case of deliberately
    resetting a company's history.
    """
    profiles, errors = load_enabled()
    for err in errors:
        print(f"[profile error] {err}", file=sys.stderr)

    if not force:
        already = [p.name for p in profiles if not _needs_seed(p.slug)]
        profiles = [p for p in profiles if _needs_seed(p.slug)]
        if already:
            print(f"[seed] already seeded, skipped: {', '.join(already)} "
                  "(use --force to re-seed and reset their first_seen)")
    if not profiles:
        print("[seed] nothing to seed")
        return

    outcomes = fetch_all(profiles,
                         deadline=time.monotonic() + _fetch_budget_seconds())

    for profile in profiles:
        outcome = outcomes.get(profile.slug)
        if outcome is None or not outcome.ok:
            # "not fetched" means the budget ran out first: seeding is
            # per-company and idempotent, so re-running --seed picks up
            # exactly the companies that are still missing.
            error = ("not fetched - budget ran out, run --seed again"
                     if outcome is None else outcome.error)
            print(f"[seed error] {profile.slug}: {error}", file=sys.stderr)
            continue
        # No filter chain here, deliberately: a seed sends nothing, so there
        # is nothing to filter, and running the chain would fire one detail
        # request per existing posting - hundreds on day one - to decide the
        # visibility of alerts that are never sent.
        seed_company(profile.slug, outcome.jobs)
        print(f"[seed] {profile.slug}: {len(outcome.jobs)} jobs saved, no alert sent")


def _crossed_threshold(slug: str) -> bool:
    """Whether this company's failures have reached the alert threshold.

    Wrapped because it touches the state file, and the failure path is
    precisely where that file is most likely to be the thing that broke."""
    try:
        return should_alert_failure(slug)
    except Exception as e:
        print(f"[state error] {slug}: could not read the failure count ({e})",
              file=sys.stderr)
        return False


def _count_failure(slug: str) -> bool:
    """Records one failure for a company and says whether it has now crossed
    the maintenance-alert threshold.

    Same reasoning as _crossed_threshold: a failure while recording a failure
    must not escalate into ending the run for the other companies."""
    try:
        record_failure(slug)
    except Exception as e:
        print(f"[state error] {slug}: could not record the failure ({e})",
              file=sys.stderr)
        return False
    return _crossed_threshold(slug)


def _rewind_after_failed_send(profile, previous_states: dict, job_count: int,
                              send_failures: list) -> None:
    """Undoes one company's state write after its alert could not be sent.

    *** Why this replaced "make the whole run fatal" ***

    State is written BEFORE notification (deliberately - see filters.py), so a
    failed send leaves jobs marked "seen" that the user never saw. The
    original answer was to exit the run non-zero so check.yml's commit step
    never ran and the ephemeral runner threw every state change away. That
    worked, and it had a collateral cost that only shows up at scale: it
    discarded the state of EVERY company, so one company's failed send re-sent
    every other company's new jobs on the next run. At three companies that is
    a duplicate or two; at a hundred it is a flood, on every run, until the
    failing company is fixed.

    Rewinding just the affected company gives the same guarantee - those jobs
    are un-seen, so they are re-detected and re-sent next run - and leaves the
    other 99 companies' work committed.

    If the rewind itself fails, the company is recorded in `send_failures`,
    and the run falls back to the old behaviour: exit non-zero, commit
    nothing. Losing a relevant job is the one outcome worth failing the whole
    run over."""
    previous = previous_states.get(profile.slug)
    try:
        if previous is None:
            raise KeyError("no pre-run snapshot for this company")
        restore_state(profile.slug, previous)
        rewound = True
    except Exception as e:
        print(f"[send error] {profile.slug}: could not rewind state ({e}); "
              "falling back to failing the whole run so nothing is committed",
              file=sys.stderr)
        send_failures.append(profile.slug)
        rewound = False

    try:
        # Best effort, and a much simpler message than the one that just
        # failed - so the user hears "something broke" instead of just going
        # quiet.
        notify_maintenance(
            profile.slug,
            f"Failed to send the new-jobs alert ({job_count} jobs). They were "
            "NOT lost - this company's state was rewound, so they will be "
            "re-detected and re-sent next run."
            if rewound else
            f"Failed to send the new-jobs alert ({job_count} jobs). They were "
            "NOT lost - state is not committed on a failed run, so they will "
            "be re-detected and re-sent next run.")
    except Exception as inner:
        print(f"[send error] {profile.slug}: maintenance alert also failed: "
              f"{inner}", file=sys.stderr)


def _run_normal() -> None:
    try:
        process_commands()
    except Exception as e:
        # A Telegram error while processing commands must not prevent the
        # job check itself from running
        print(f"[commands error] {e}", file=sys.stderr)

    profiles, errors = load_enabled()
    for err in errors:
        print(f"[profile error] {err}", file=sys.stderr)
        # Not sent to Telegram - a validation error is a bug in the
        # profile, not an operational event that needs Orel's immediate
        # attention. It'll show up in the run's logs.

    # Built once for the whole run, from the settings file the bot itself
    # writes over Telegram. Only enabled filters are constructed, so an
    # empty chain means literally no extra work anywhere below.
    chain = build_chain()
    run_stats = RunStats()
    # One detail-fetch allowance for the WHOLE run, shared by every company -
    # see detail.DetailBudget.
    detail_budget = DetailBudget()

    had_seed_gap = []
    unreadable_state = []
    send_failures = []
    not_fetched = []

    # The seed-gap check is done BEFORE fetching, not inside the loop below,
    # so an unseeded company costs no request at all. It is a local file read,
    # so doing it up front is free.
    #
    # It doubles as a readability check on every state file, which is why an
    # unreadable one is caught here rather than blowing up mid-run: this loop
    # used to let a json.JSONDecodeError escape, and one damaged file took the
    # entire run down before a single company had been fetched.
    eligible = []
    previous_states = {}
    for profile in profiles:
        try:
            state = load_state(profile.slug)
        except StateUnreadable as e:
            print(f"[state error] {e}", file=sys.stderr)
            unreadable_state.append(profile.name)
            continue
        if state.get("last_success") is None:
            # No state at all - this company was never seeded. Don't run
            # a diff (everything would look "new"); report and skip
            # instead - deliberately, per the decision not to auto-seed.
            had_seed_gap.append(profile.name)
            continue
        # Kept so a failed send can rewind this company - and only this
        # company - to exactly where it started. See the closing note.
        previous_states[profile.slug] = state
        eligible.append(profile)

    # *** The only concurrent phase of the run. ***
    # Everything below this line runs sequentially, in profile order, exactly
    # as it did when the fetch was inline: the state write, the filter chain's
    # shared counters and the Telegram sends are all order- or rate-sensitive,
    # and none of them are worth parallelising for what they cost. See
    # fetchers.fetch_all.
    outcomes = fetch_all(eligible,
                         deadline=time.monotonic() + _fetch_budget_seconds())

    for profile in eligible:
        outcome = outcomes.get(profile.slug)
        if outcome is None:
            # The fetch budget ran out before this company started. Not a
            # failure: nothing was tried, so nothing broke. No counter bump,
            # no maintenance alert - just a line in the log and a retry on the
            # next run, three hours later.
            not_fetched.append(profile.name)
            continue

        if not outcome.ok:
            print(f"[fetch error] {profile.slug}: {outcome.error}", file=sys.stderr)
            # Counted, not just logged - see state.record_failure. A fetch
            # that raises is the same operational event as a suspicious zero,
            # and has to reach the same threshold.
            if _count_failure(profile.slug):
                notify_maintenance(profile.slug, f"Fetch error: {outcome.error}")
            continue

        # One company must never be able to end the run. Everything below has
        # its own narrower handler already; this catches what none of them
        # anticipated (a disk error on the state write, an unexpected profile
        # shape) and keeps it costing exactly one company, the same as a fetch
        # error does.
        try:
            result = process_company(profile.slug, outcome.jobs, profile)
        except Exception as e:
            print(f"[process error] {profile.slug}: {e}", file=sys.stderr)
            _count_failure(profile.slug)
            continue

        if result.status == "empty_suspicious":
            print(f"[health gate] {result.message}", file=sys.stderr)
            # process_company already bumped the counter on its way out, so
            # this only reads it.
            if _crossed_threshold(profile.slug):
                notify_maintenance(profile.slug, result.message)
            continue

        if result.new_jobs:
            # process_company has ALREADY written every new id to state by
            # this point. The chain only decides what gets shown - a job
            # rejected here is still permanently "seen", so it will never be
            # re-detected or re-fetched on a later run.
            try:
                survivors = run_chain(result.new_jobs, profile, chain,
                                      run_stats, detail_budget)
            except Exception as e:
                # The filter layer must never cost an alert. If the chain
                # itself breaks, fall back to sending everything untagged -
                # the same fail-open direction every decision inside it takes.
                print(f"[filter error] {profile.slug}: {e}", file=sys.stderr)
                survivors = [(job, None) for job in result.new_jobs]

            if survivors:
                jobs_to_send = [job for job, _ in survivors]
                tags = {job.id: tag for job, tag in survivors if tag}
                try:
                    notify_new_jobs(profile.name, jobs_to_send, tags)
                except Exception as e:
                    print(f"[send error] {profile.slug}: {e}", file=sys.stderr)
                    _rewind_after_failed_send(profile, previous_states,
                                              len(jobs_to_send), send_failures)

            suppressed = len(result.new_jobs) - len(survivors)
            if suppressed:
                print(f"[filter] {profile.slug}: {suppressed} of "
                      f"{len(result.new_jobs)} new jobs suppressed")

    # Written after every company, so the counters cover the whole run.
    save_stats(run_stats)

    if had_seed_gap:
        names = ", ".join(had_seed_gap)
        print(f"[seed gap] manual `python run.py --seed` needed for: {names}",
             file=sys.stderr)

    if unreadable_state:
        names = ", ".join(unreadable_state)
        print(f"[state gap] unreadable state files, skipped this run: {names}. "
              "Restore the file from git history - do NOT re-seed, that would "
              "mark every currently-open posting as already known.",
              file=sys.stderr)

    if not_fetched:
        names = ", ".join(not_fetched)
        print(f"[budget] not fetched this run (the fetch budget ran out "
              f"first): {names}. Retried next run.", file=sys.stderr)

    # *** Why a failed send is fatal only when the rewind failed ***
    #
    # State is written BEFORE notification, so by this point the jobs whose
    # alert failed are already recorded as "seen" on disk. Committing that
    # would be permanent - the jobs would never be re-detected and never
    # delivered. Silently losing a relevant job is the single outcome this
    # whole project is built to prevent.
    #
    # The normal answer is now a per-company rewind (see
    # _rewind_after_failed_send), which un-sees exactly the affected jobs and
    # lets every other company commit. This exit is the fallback for when even
    # that failed: exiting non-zero skips check.yml's commit step, the
    # ephemeral runner discards every state change, the previous committed
    # state survives, and the next run re-detects and retries. A noisy retry
    # is strictly better than a quiet loss.
    if send_failures:
        print(f"[run failed] alerts could not be sent, and state could not be "
              f"rewound, for: {', '.join(send_failures)}. Exiting non-zero on "
              "purpose so state is NOT committed and these jobs are retried "
              "next run.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if "--seed" in sys.argv:
        _run_seed(force="--force" in sys.argv)
    else:
        _run_normal()
