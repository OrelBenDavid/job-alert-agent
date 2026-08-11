# -*- coding: utf-8 -*-
"""
Main entry point. Two ways to run it:

    python run.py            normal run: Telegram commands -> job check -> alerts
    python run.py --seed     manual seed: fetches everything, writes state,
                             sends nothing

--seed exists for two situations: (a) the initial repo, one run right after
deployment, before the cron is turned on. (b) adding a new company - so it
doesn't blast a message with all of its existing jobs the moment it's added.
It was explicitly decided NOT to auto-seed when "no state" is detected -
so that a state file deleted by accident surfaces as an error (the health
gate raises it clearly, it doesn't stay silent) instead of being silently
swallowed as "a new company".
"""

import sys

from profiles import load_enabled
from fetchers import fetch_jobs
from state import process_company, seed_company, should_alert_failure, load_state
from notifier import notify_new_jobs, notify_maintenance
from commands import process_commands


def _run_seed() -> None:
    profiles, errors = load_enabled()
    for err in errors:
        print(f"[profile error] {err}", file=sys.stderr)

    for profile in profiles:
        try:
            jobs = fetch_jobs(profile)
        except Exception as e:
            print(f"[seed error] {profile.slug}: {e}", file=sys.stderr)
            continue
        seed_company(profile.slug, jobs)
        print(f"[seed] {profile.slug}: {len(jobs)} jobs saved, no alert sent")


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

    had_seed_gap = []
    for profile in profiles:
        state = load_state(profile.slug)
        if state.get("last_success") is None:
            # No state at all - this company was never seeded. Don't run
            # a diff (everything would look "new"); report and skip
            # instead - deliberately, per the decision not to auto-seed.
            had_seed_gap.append(profile.name)
            continue

        try:
            jobs = fetch_jobs(profile)
        except Exception as e:
            print(f"[fetch error] {profile.slug}: {e}", file=sys.stderr)
            if should_alert_failure(profile.slug):
                notify_maintenance(profile.slug, f"Fetch error: {e}")
            continue

        result = process_company(profile.slug, jobs, profile)

        if result.status == "empty_suspicious":
            print(f"[health gate] {result.message}", file=sys.stderr)
            if should_alert_failure(profile.slug):
                notify_maintenance(profile.slug, result.message)
            continue

        if result.new_jobs:
            notify_new_jobs(profile.name, result.new_jobs)

    if had_seed_gap:
        names = ", ".join(had_seed_gap)
        print(f"[seed gap] manual `python run.py --seed` needed for: {names}",
             file=sys.stderr)


if __name__ == "__main__":
    if "--seed" in sys.argv:
        _run_seed()
    else:
        _run_normal()
