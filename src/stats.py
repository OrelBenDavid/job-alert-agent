# -*- coding: utf-8 -*-
"""
Filter counters - a required part of the feature, not a nice-to-have.

The design was built on US aggregate data (Indeed Hiring Lab, Apr 2024:
only ~30% of postings state a specific number of years). Whether that holds
for Israeli tech postings is unknown, and it is the single number that
decides whether this filter is genuinely working or merely appearing to.
Without these counters there is no way to tell the difference: a parser that
silently returns None for everything looks exactly like a healthy run that
happens to reject nothing.

Five buckets, because the interesting failure modes are distinguishable:
  rejected_by_title    - suppressed with zero requests (the cheap win)
  passed_with_number   - a number was read, and it was low enough
  rejected_with_number - a number was read, and it was too high (the point)
  undetermined         - no number anywhere
  undetermined_signals - no number, but the posting reads senior

If undetermined + undetermined_signals dwarfs the two "with_number" buckets,
the parser is not doing its job regardless of how few alerts get through.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import json
from datetime import datetime, timezone
from pathlib import Path

STATS_PATH = Path(__file__).resolve().parent.parent / "state" / "filter_stats.json"

COUNTER_NAMES = ("rejected_by_title", "passed_with_number",
                 "rejected_with_number", "undetermined", "undetermined_signals")


def _empty_counters() -> dict:
    return {name: 0 for name in COUNTER_NAMES}


class RunStats:
    """Counters for a single run, keyed by filter name.

    Keyed by filter rather than flat so a second filter added to the chain
    later gets its own counters for free, without a schema change here."""

    def __init__(self) -> None:
        self.by_filter: dict[str, dict] = {}

    def record(self, filter_name: str, counter: str) -> None:
        counters = self.by_filter.setdefault(filter_name, _empty_counters())
        # setdefault on the counter too: an unknown bucket name should show up
        # in the output rather than raising in the middle of a live run.
        counters[counter] = counters.get(counter, 0) + 1

    def total(self, filter_name: str) -> int:
        return sum(self.by_filter.get(filter_name, {}).values())

    def is_empty(self) -> bool:
        return not any(self.by_filter.values())


def load_stats() -> dict:
    """Last run's counters plus running totals. Missing file = all zeros."""
    if not STATS_PATH.exists():
        return {"last_run_at": None, "last_run": {}, "totals": {}}
    try:
        return json.loads(STATS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_run_at": None, "last_run": {}, "totals": {}}


def save_stats(run: RunStats) -> None:
    """Records this run and folds it into the running totals.

    A run where the chain did nothing at all (filter off, or no new jobs) is
    NOT written: overwriting last_run with zeros would erase the most recent
    real measurement, which is the one worth looking at."""
    if run.is_empty():
        return

    stored = load_stats()
    totals = stored.get("totals") or {}

    for filter_name, counters in run.by_filter.items():
        filter_totals = totals.setdefault(filter_name, _empty_counters())
        for counter, value in counters.items():
            filter_totals[counter] = filter_totals.get(counter, 0) + value

    stored["last_run_at"] = datetime.now(timezone.utc).isoformat()
    stored["last_run"] = run.by_filter
    stored["totals"] = totals

    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(STATS_PATH)
