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

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import argparse
import os
import sys
import time

from profiles import load_enabled
from fetchers import fetch_all
from state import (process_company, seed_company, should_alert_failure,
                   load_state, record_failure, restore_state, StateUnreadable,
                   flood_decision, record_flood_suppressed,
                   clear_flood_counter, FLOOD_ACCEPT_AFTER)
from notifier import notify_new_jobs, notify_maintenance
from commands import process_commands
from detail import DetailBudget
from filters import build_chain, run_chain, recently_seen_titles
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


def _read_company_list(path: str) -> list[str]:
    """Slugs from a plain-text file, one per line. `#` comments and blank
    lines are ignored, so a batch file can carry a note about why."""
    slugs = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if line:
                slugs.append(line)
    return slugs


def _run_seed(force: bool = False, limit: int | None = None,
              only: str | None = None) -> None:
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

    *** Why batching exists ***

    `--limit N` seeds only the first N companies still needing it, and
    `--only <file>` seeds a named list. Seeding is the one irreversible step
    in adding a company - it decides which postings are treated as "already
    known" and therefore never alerted - and doing 139 companies in one pass
    records ~1,350 of those decisions with no chance to look in between.
    Because the command is idempotent and skips what is already seeded,
    running it repeatedly with --limit walks through the backlog in
    reviewable chunks, and the order is stable (profiles load sorted by
    path), so the batches are reproducible rather than arbitrary.
    """
    profiles, errors = load_enabled()
    for err in errors:
        print(f"[profile error] {err}", file=sys.stderr)

    if only:
        wanted = set(_read_company_list(only))
        missing = wanted - {p.slug for p in profiles}
        if missing:
            # Named but absent: a typo'd slug must not be silently skipped, or
            # the company looks seeded when nothing happened to it.
            print(f"[seed] not found (or disabled): {', '.join(sorted(missing))}",
                  file=sys.stderr)
        profiles = [p for p in profiles if p.slug in wanted]

    if not force:
        already = [p.name for p in profiles if not _needs_seed(p.slug)]
        profiles = [p for p in profiles if _needs_seed(p.slug)]
        if already:
            print(f"[seed] already seeded, skipped: {len(already)} companies "
                  "(use --force to re-seed and reset their first_seen)")

    remaining = 0
    if limit is not None and len(profiles) > limit:
        remaining = len(profiles) - limit
        profiles = profiles[:limit]

    if not profiles:
        print("[seed] nothing to seed")
        return

    print(f"[seed] seeding {len(profiles)} companies"
          + (f", {remaining} left for the next run" if remaining else ""))
    outcomes = fetch_all(profiles,
                         deadline=time.monotonic() + _fetch_budget_seconds())

    seeded, total_jobs = 0, 0
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
        seeded += 1
        total_jobs += len(outcome.jobs)
        print(f"[seed] {profile.slug}: {len(outcome.jobs)} jobs saved, no alert sent")

    print(f"[seed] batch done: {seeded} companies, {total_jobs} postings "
          "recorded as already-known (no alerts sent)")
    if remaining:
        print(f"[seed] {remaining} companies still unseeded - run --seed again "
              "to continue")


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


def _rewind_suppressed_flood(pending: list, previous_states: dict) -> int:
    """Un-sees every job in a flood that was withheld. Returns how many
    companies could not be rewound.

    This is what makes withholding safe. State is written before notification,
    so a withheld batch is already recorded as seen; without this the gate
    would trade a noisy flood for a silent, permanent loss of every job in it -
    strictly the worse outcome, and the one the whole project is built to
    avoid."""
    failed = 0
    for profile, _jobs, _tags in pending:
        previous = previous_states.get(profile.slug)
        try:
            if previous is None:
                raise KeyError("no pre-run snapshot for this company")
            restore_state(profile.slug, previous)
        except Exception as e:
            print(f"[flood gate] {profile.slug}: could not rewind state ({e})",
                  file=sys.stderr)
            failed += 1
    return failed


def _deliver(pending: list, previous_states: dict,
             send_failures: list, corpus_postings: int | None = None) -> None:
    """Sends every company's alert, unless the run's total volume is
    implausible - see state.flood_decision.

    `corpus_postings` is how many postings this run saw in total, across every
    company and not only the ones with something new. It is what scales the
    flood threshold, so that adding companies cannot tighten the gate - see
    state.IMPLAUSIBLE_NEW_JOBS_FRACTION.

    Sending happens here, after the whole loop, rather than per company inside
    it. That is the only way the volume gate can exist at all: it is a
    judgement about the run, and by the time company #1 has been sent it is
    too late to decide the run was a flood."""
    total = sum(len(jobs) for _, jobs, _ in pending)
    if not total:
        clear_flood_counter()
        return

    suppress, message = flood_decision(total, corpus_postings)

    if suppress:
        rewind_failures = _rewind_suppressed_flood(pending, previous_states)
        count = record_flood_suppressed()
        print(f"[flood gate] {total} new jobs across {len(pending)} companies "
              f"withheld (run {count} of {FLOOD_ACCEPT_AFTER}); state rewound",
              file=sys.stderr)
        # The biggest contributors, so the message says something actionable
        # about WHICH companies produced the volume.
        top = sorted(pending, key=lambda p: -len(p[1]))[:5]
        detail = ", ".join(f"{p.name} ({len(jobs)})" for p, jobs, _ in top)
        try:
            notify_maintenance(
                "run", f"{message}\n\nBiggest contributors: {detail}")
        except Exception as e:
            print(f"[send error] flood warning failed: {e}", file=sys.stderr)

        if rewind_failures:
            # Same fallback as a failed send: if the jobs could not be un-seen,
            # the only way to stop them being lost is to commit nothing.
            send_failures.append(f"{rewind_failures} companies (flood rewind)")
        return

    if message:
        # Accepted after holding out - said once, plainly, before the batch.
        print(f"[flood gate] {message}", file=sys.stderr)
        try:
            notify_maintenance("run", message)
        except Exception as e:
            print(f"[send error] flood note failed: {e}", file=sys.stderr)

    for profile, jobs, tags in pending:
        try:
            notify_new_jobs(profile.name, jobs, tags)
        except Exception as e:
            print(f"[send error] {profile.slug}: {e}", file=sys.stderr)
            _rewind_after_failed_send(profile, previous_states, len(jobs),
                                      send_failures)

    clear_flood_counter()


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
    # (profile, jobs, tags) per company with something to send. Held until
    # every company is processed - see _deliver.
    pending = []

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

        if result.message:
            # A healthy run that still has something to say - today only an
            # accepted collapse. Sent unconditionally rather than behind the
            # repeated-failure threshold: it is a one-off statement of fact,
            # not a symptom that might clear on its own, and it is the last
            # thing the user hears about a drop before the bot resumes
            # treating the lower count as normal.
            print(f"[health gate] {result.message}", file=sys.stderr)
            try:
                notify_maintenance(profile.slug, result.message)
            except Exception as e:
                print(f"[send error] {profile.slug}: collapse note failed: "
                      f"{e}", file=sys.stderr)

        if result.new_jobs:
            # process_company has ALREADY written every new id to state by
            # this point. The chain only decides what gets shown - a job
            # rejected here is still permanently "seen", so it will never be
            # re-detected or re-fetched on a later run.
            try:
                # The PRE-run snapshot, not the state on disk: process_company
                # has already written this run's new ids, so reading it now
                # would match every new job against its own title.
                seen_titles = recently_seen_titles(
                    previous_states.get(profile.slug) or {})
                survivors = run_chain(result.new_jobs, profile, chain,
                                      run_stats, detail_budget, seen_titles)
            except Exception as e:
                # The filter layer must never cost an alert. If the chain
                # itself breaks, fall back to sending everything untagged -
                # the same fail-open direction every decision inside it takes.
                print(f"[filter error] {profile.slug}: {e}", file=sys.stderr)
                survivors = [(job, None) for job in result.new_jobs]

            if survivors:
                # Collected rather than sent here. The implausible-volume gate
                # below is a decision about the RUN, so it cannot be made until
                # every company has been processed - sending inside this loop
                # would already have delivered the first companies' share of a
                # flood before the total was known.
                pending.append((profile,
                                [job for job, _ in survivors],
                                {job.id: tag for job, tag in survivors if tag}))

            suppressed = len(result.new_jobs) - len(survivors)
            if suppressed:
                print(f"[filter] {profile.slug}: {suppressed} of "
                      f"{len(result.new_jobs)} new jobs suppressed")

    # Written after every company, so the counters cover the whole run.
    save_stats(run_stats)

    # The denominator for the flood gate: every posting this run actually saw,
    # not just the new ones. Counted from the outcomes rather than from the
    # profile list, so a run whose budget ran out judges itself against the
    # corpus it really fetched instead of the one it hoped to.
    corpus_postings = sum(len(o.jobs) for o in outcomes.values()
                          if o is not None and o.ok and o.jobs)

    _deliver(pending, previous_states, send_failures, corpus_postings)

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


def _parse_args(argv: list[str]):
    """argparse rather than the old `"--seed" in sys.argv`, now that seed
    takes options. --limit and --only are seed-only on purpose: there is no
    coherent meaning for "check a quarter of the companies", and offering it
    would invite a partial scheduled run that looks complete."""
    parser = argparse.ArgumentParser(
        description="Job alert agent. No arguments = a normal run.")
    parser.add_argument("--seed", action="store_true",
                        help="record current postings as already-known and "
                             "send nothing. Manual only, never scheduled.")
    parser.add_argument("--force", action="store_true",
                        help="with --seed: re-seed companies that already "
                             "have state, resetting their first_seen.")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="with --seed: seed at most N companies, then "
                             "stop. Re-run to continue with the next batch.")
    parser.add_argument("--only", default=None, metavar="FILE",
                        help="with --seed: seed only the slugs listed in "
                             "FILE, one per line (# comments allowed).")
    args = parser.parse_args(argv)
    if not args.seed and (args.limit is not None or args.only or args.force):
        parser.error("--limit, --only and --force only apply with --seed")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    if args.seed:
        _run_seed(force=args.force, limit=args.limit, only=args.only)
    else:
        _run_normal()
