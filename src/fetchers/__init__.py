# -*- coding: utf-8 -*-
"""
The one dispatcher in the project: fetch_type -> module. That's all there
is - no slug->function mapping and no companies.json. A new company on an
existing platform/strategy doesn't touch this file at all, it only adds
profiles/<slug>.json.

This module also owns the run's CONCURRENCY, for one reason: fetching is the
only phase of a run that is safe to parallelise. Everything downstream of it
- the diff, the state write, the filter chain, the Telegram send - stays
strictly sequential in run.py, so none of the ordering guarantees that
pipeline depends on are affected by anything here. See fetch_all.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from models import Job
from . import api as _api
from . import html as _html
from . import browser as _browser

_FETCH_TYPE_DISPATCH = {
    "api": _api.fetch,
    "html": _html.fetch,
    "playwright": _browser.fetch,
}

# Two pools, not one, because the two kinds of fetch are limited by opposite
# resources and a single pool would have to be sized for the worse of them.
#
#   network (api/html) - one or a few `requests` calls, a few hundred KB of
#     RAM, essentially all of it spent waiting on a socket. Cheap enough to
#     run wide; the only real ceiling is politeness to the remote host, and
#     each company here is a DIFFERENT host.
#
#   browser (playwright) - a whole Chromium per company, hundreds of MB and
#     a real CPU share. A GitHub-hosted runner on a private repo is 2 vCPU /
#     7 GB, so this has to stay small. Oversubscribing it does not merely run
#     slower: contention pushes the first-page selector wait past its budget,
#     which is exactly the failure browser.ListingNeverRendered exists to
#     catch. Measured: 3 concurrent Chromiums against careers.wix.com on a
#     4-core laptop turned a healthy 16-job fetch into 0 jobs on all three.
#
# Both are env-overridable so the numbers can be tuned from the workflow
# file without a code change - the runner's size is a deployment fact, not a
# property of the code.
DEFAULT_NETWORK_WORKERS = 12
DEFAULT_BROWSER_WORKERS = 2

_BROWSER_FETCH_TYPES = ("playwright",)


@dataclass
class FetchOutcome:
    """What one company's fetch produced. Exactly one of jobs/error is set.

    The error is CARRIED rather than raised, because a pool that propagated
    the first exception would abandon the other 99 companies' results. run.py
    re-raises it into its existing per-company handler, so one company's dead
    selector still costs only that company."""
    slug: str
    jobs: list[Job] | None
    error: Exception | None
    seconds: float

    @property
    def ok(self) -> bool:
        return self.error is None


def _workers(env_name: str, default: int) -> int:
    """Reads a worker count from the environment, clamped to at least 1.

    A bad value falls back to the default rather than raising: a typo in a
    workflow env var must not be able to take a scheduled run down."""
    try:
        return max(1, int(os.environ.get(env_name, default)))
    except (TypeError, ValueError):
        return default


def fetch_jobs(profile) -> list[Job]:
    """The single-company entry point. profile is a src.profiles.Profile.
    Raises NotImplementedError if fetch_type is unknown - shouldn't happen in
    practice since profiles.py already validates this at load time, but the
    double check is cheap and prevents odd silent behavior if something
    changed between the two stages."""
    handler = _FETCH_TYPE_DISPATCH.get(profile.fetch_type)
    if handler is None:
        raise NotImplementedError(
            f"{profile.slug}: unknown fetch_type: {profile.fetch_type!r}")
    return handler(profile)


def _fetch_one(profile) -> FetchOutcome:
    """Runs one company's fetch and captures its result or its exception.

    Nothing in here touches shared state - no state file, no counters, no
    Telegram. That is what makes the pool safe, and it is worth keeping true:
    anything added to this function has to be thread-safe, whereas anything
    added to run.py's sequential loop does not."""
    started = time.time()
    try:
        jobs = fetch_jobs(profile)
        return FetchOutcome(profile.slug, jobs, None, time.time() - started)
    except Exception as e:
        return FetchOutcome(profile.slug, None, e, time.time() - started)


def fetch_all(profiles) -> dict[str, FetchOutcome]:
    """Fetches every company concurrently. Returns outcomes keyed by slug.

    *** Why only this phase is concurrent ***

    A run's cost is almost entirely this loop - a browser company measures
    ~30s against ~1s for an API one - and every company is an independent
    call to a different host, so it is embarrassingly parallel. Everything
    after it is not: the state write, the filter chain's counters and the
    Telegram send are all shared, ordered, or rate-limited. Parallelising
    only the fetch buys nearly all of the wall-clock win at none of that
    risk, and leaves run.py's documented ordering (state written BEFORE
    filtering, a failed send fatal) reading exactly as it did.

    The caller iterates its own profile list to consume this dict, so results
    are still PROCESSED in profile order however the pool happened to
    schedule them. Logs, state writes and alert order stay deterministic and
    reviewable; only the waiting overlaps.
    """
    if not profiles:
        return {}

    browser = [p for p in profiles if p.fetch_type in _BROWSER_FETCH_TYPES]
    network = [p for p in profiles if p.fetch_type not in _BROWSER_FETCH_TYPES]

    n_workers = min(_workers("JOB_ALERT_NETWORK_WORKERS", DEFAULT_NETWORK_WORKERS),
                    max(1, len(network)))
    b_workers = min(_workers("JOB_ALERT_BROWSER_WORKERS", DEFAULT_BROWSER_WORKERS),
                    max(1, len(browser)))

    started = time.time()
    outcomes: dict[str, FetchOutcome] = {}

    # The two pools run AT THE SAME TIME, not one after the other: the
    # network pool is idle on a socket while the browser pool is busy on the
    # CPU, so overlapping them is close to free and saves the whole cheaper
    # phase's wall clock.
    with ThreadPoolExecutor(max_workers=n_workers,
                            thread_name_prefix="fetch-net") as net_pool, \
         ThreadPoolExecutor(max_workers=b_workers,
                            thread_name_prefix="fetch-browser") as browser_pool:
        futures = [net_pool.submit(_fetch_one, p) for p in network]
        futures += [browser_pool.submit(_fetch_one, p) for p in browser]
        for future in futures:
            outcome = future.result()   # _fetch_one never raises - it carries
            outcomes[outcome.slug] = outcome

    elapsed = time.time() - started
    failed = sum(1 for o in outcomes.values() if not o.ok)
    slowest = max(outcomes.values(), key=lambda o: o.seconds)
    print(f"[fetch] {len(profiles)} companies in {elapsed:.1f}s "
          f"({len(network)} network x{n_workers}, {len(browser)} browser "
          f"x{b_workers}); {failed} failed; slowest {slowest.slug} "
          f"{slowest.seconds:.1f}s", file=sys.stderr)

    return outcomes
