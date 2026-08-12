# -*- coding: utf-8 -*-
"""
Runtime settings for the filter chain - the values the bot itself can change.

Deliberately a state file the bot writes, NOT a GitHub Actions env var and
NOT a constant in the code. The entire point is that the threshold can be
changed from a phone, over Telegram, without a commit and without waiting
for a workflow edit to land. state/ is already committed back to the repo by
check.yml, so a change made this way survives to the next run for free.

Defaults are the answer when the file doesn't exist yet, which is also the
answer on the very first run after this patch ships: the experience filter
starts ON.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "state" / "filters.json"

# The threshold the whole feature exists for: postings demanding MORE than
# this many years are suppressed. Hard, with no grey zone - a false negative
# is a relevant job the user never sees, a false positive costs two seconds
# of scrolling, and those are not remotely the same cost.
DEFAULT_MAX_YEARS = 1.0

DEFAULTS = {
    "experience": {
        "enabled": True,        # starts ON, per the design
        "max_years": DEFAULT_MAX_YEARS,
        # strict mode makes an UNDETERMINED reading reject instead of pass.
        # Off by default - it inverts the fail-open rule, so it exists purely
        # as an escape hatch if the undetermined noise turns out unbearable.
        "strict": False,
    },
}


def load_settings() -> dict:
    """Current settings, with defaults filled in for anything missing.

    Merged rather than replaced so that adding a new filter (or a new knob on
    an existing one) doesn't require migrating the on-disk file - an older
    file simply picks up the new default."""
    merged = {name: dict(values) for name, values in DEFAULTS.items()}

    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A corrupt settings file must not take the run down. Falling
            # back to defaults means the filter is ON with a 1-year
            # threshold - the same state it ships in.
            return merged
        for name, values in (stored.get("filters") or {}).items():
            if name in merged and isinstance(values, dict):
                merged[name].update(values)

    return merged


def save_settings(settings: dict) -> None:
    """Atomic write, same pattern as state.py - a run killed mid-write must
    never leave a half-written settings file that then fails to parse."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"filters": settings}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


def update_filter(name: str, **changes) -> dict:
    """Reads, applies changes to one filter, writes back. Returns the
    filter's new settings so the caller can echo them to the user."""
    settings = load_settings()
    if name not in settings:
        raise KeyError(name)
    settings[name].update(changes)
    save_settings(settings)
    return settings[name]
