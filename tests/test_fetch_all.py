"""The concurrent fetch phase.

What is actually being protected here is not speed - it is that
parallelising the fetch changed NOTHING about the contract run.py depends
on: every company still gets exactly one outcome, one company's failure is
still isolated to that company, and results are still consumable in profile
order however the pool scheduled them.
"""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import fetchers
from models import Job


class _FakeProfile:
    def __init__(self, slug, fetch_type="api"):
        self.slug = slug
        self.name = slug.title()
        self.fetch_type = fetch_type


def _job(slug, n):
    return Job(id=f"{slug}-{n}", title=f"Role {n}", location="Tel Aviv",
               url=f"https://x/{slug}/{n}", company=slug)


def test_every_profile_gets_exactly_one_outcome():
    profiles = [_FakeProfile(f"c{i}") for i in range(10)]

    with patch.object(fetchers, "fetch_jobs",
                      side_effect=lambda p: [_job(p.slug, 1)]):
        outcomes = fetchers.fetch_all(profiles)

    assert set(outcomes) == {p.slug for p in profiles}
    assert all(o.ok for o in outcomes.values())
    assert all(len(o.jobs) == 1 for o in outcomes.values())


def test_one_company_failing_does_not_affect_the_others():
    """The reason _fetch_one carries the exception instead of raising it: a
    pool that propagated would abandon the other companies' results."""
    profiles = [_FakeProfile("good1"), _FakeProfile("bad"), _FakeProfile("good2")]

    def flaky(profile):
        if profile.slug == "bad":
            raise RuntimeError("dead selector")
        return [_job(profile.slug, 1)]

    with patch.object(fetchers, "fetch_jobs", side_effect=flaky):
        outcomes = fetchers.fetch_all(profiles)

    assert outcomes["good1"].ok and outcomes["good2"].ok
    assert not outcomes["bad"].ok
    assert isinstance(outcomes["bad"].error, RuntimeError)
    assert outcomes["bad"].jobs is None
    # and the error is still readable downstream, since run.py logs it
    assert "dead selector" in str(outcomes["bad"].error)


def test_empty_input_does_no_work():
    """100 companies all unseeded is a real state - it must not spin up pools
    or emit a summary line about zero companies."""
    assert fetchers.fetch_all([]) == {}


def test_browser_and_network_use_separate_pools():
    """The whole point of two pools: a slow Chromium company must not hold a
    slot that an api company could be using. Verified by checking the two
    kinds run on differently-named threads, which is what bounds them
    separately."""
    profiles = ([_FakeProfile(f"api{i}", "api") for i in range(4)]
                + [_FakeProfile(f"br{i}", "playwright") for i in range(2)])
    threads_used = {}

    def record(profile):
        threads_used[profile.slug] = threading.current_thread().name
        return [_job(profile.slug, 1)]

    with patch.object(fetchers, "fetch_jobs", side_effect=record):
        fetchers.fetch_all(profiles)

    assert all("fetch-net" in threads_used[f"api{i}"] for i in range(4))
    assert all("fetch-browser" in threads_used[f"br{i}"] for i in range(2))


def test_browser_pool_is_bounded():
    """Not a performance assertion - a correctness one. Exceeding the browser
    bound is what produced the 0-jobs-under-contention failure this pool was
    sized to prevent."""
    profiles = [_FakeProfile(f"br{i}", "playwright") for i in range(6)]
    live = 0
    peak = 0
    lock = threading.Lock()

    def slow(profile):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return [_job(profile.slug, 1)]

    with patch.dict("os.environ", {"JOB_ALERT_BROWSER_WORKERS": "2"}), \
         patch.object(fetchers, "fetch_jobs", side_effect=slow):
        outcomes = fetchers.fetch_all(profiles)

    assert peak <= 2, f"ran {peak} browsers at once, bound was 2"
    assert len(outcomes) == 6


def test_it_really_runs_concurrently():
    """Guards against the pools being accidentally serialised - the entire
    reason this module exists. 8 companies x 0.1s is 0.8s sequentially."""
    profiles = [_FakeProfile(f"c{i}") for i in range(8)]

    def slow(profile):
        time.sleep(0.1)
        return [_job(profile.slug, 1)]

    with patch.object(fetchers, "fetch_jobs", side_effect=slow):
        started = time.time()
        fetchers.fetch_all(profiles)
        elapsed = time.time() - started

    assert elapsed < 0.5, f"took {elapsed:.2f}s - the pool is not parallel"


def test_a_bad_worker_env_var_falls_back_instead_of_crashing():
    """A typo in the workflow file must not be able to kill a scheduled run."""
    with patch.dict("os.environ", {"JOB_ALERT_NETWORK_WORKERS": "not-a-number"}), \
         patch.object(fetchers, "fetch_jobs", side_effect=lambda p: []):
        outcomes = fetchers.fetch_all([_FakeProfile("c1")])
    assert outcomes["c1"].ok
