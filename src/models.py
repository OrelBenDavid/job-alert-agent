# -*- coding: utf-8 -*-
"""
The core data model of the project. Every fetcher - of any kind - returns
list[Job]. Diffing, deduplication, and "new job" detection always run on
Job.id, never on display text.

A Job.to_dict()/from_dict() pair used to live here, documented as "the
serialization for state/seen/<slug>.json". Nothing ever called either one -
state.py has always written its own two-field record directly - so both were
removed on 2026-08-19. They were worse than merely unused: a future change to
the state format would have been made there, correctly, and had no effect.
"""

# Deferred annotation evaluation: lets the `X | None` spelling below work on
# any interpreter the repo might be run on locally, while CI stays on 3.12.
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Job:
    """A single job posting. frozen=True so it can be passed around safely
    between layers without accidental mutation (in practice, dicts keyed
    by .id are used for comparisons, not the object identity itself)."""

    id: str          # Stable identifier: the ATS's own id, or a canonicalized
                      # link as a fallback. This is the ONLY field the diff
                      # looks at - never the title.
    title: str        # Job title as shown on the site
    location: str      # Raw location string, kept as-is for display/debugging
    url: str          # Direct link to the job page - what gets sent in the
                       # Telegram alert
    company: str      # The company's slug (matches profiles/<slug>.json),
                      # to trace a job back to its source

    # --- Fields below are filled in AFTER the diff, by the filter layer. ---
    # compare=False is load-bearing, not cosmetic: without it, two Job objects
    # for the same posting would stop being equal the moment one of them got
    # enriched with a description, and any future equality-based comparison
    # would start reporting phantom "new" jobs. The diff itself runs on .id
    # (see state.process_company), and these fields are excluded from
    # state's own writer as well (state.process_company builds its `jobs` map
    # from .id/.title only) - nothing about them ever reaches state/seen/*.json.
    description: str | None = field(default=None, compare=False, repr=False)
    # The minimum required years of experience parsed out of `description`.
    # None means "undetermined" - which is a PASS (fail-open), never a reject.
    min_years_exp: float | None = field(default=None, compare=False)

    def display(self) -> str:
        """The agreed display format only. Built here, never stored in
        state, and never used as a comparison key - otherwise a cosmetic
        change on the company's side (a space becoming a hyphen, "(Hybrid)"
        appended to a title) would look like a brand-new job."""
        return f"{self.title} — {self.location}"
