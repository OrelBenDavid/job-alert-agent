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


class JobList(list):
    """A fetcher's result, carrying how big the WHOLE board was.

    *** The denominator, and why the project needed one - added 2026-08-23 ***

    Every count in this project has been post-filter: `last_count` is the
    number of ISRAEL-RELEVANT postings, and nothing has ever recorded how many
    postings the board held before relevance was decided. Without that number,
    two completely different situations are the same observation:

        a real zero          the board returns 41 postings, none in Israel
        the wrong board      the board returns 2 postings, because the
                             endpoint points somewhere that isn't the
                             company's careers board at all

    Both read as `last_count: 0`, both are stable, and both look healthy. The
    corpus audit on 2026-08-23 found the second case four times, one of them
    (`wiz`, board_token 'wizprivate' with two postings on it, against
    'wizinc' with 124) sitting in the repo verified and green for eleven days,
    with a test asserting the mis-resolution was a fact about the company.
    Nothing in the system could have caught it, because nothing counted the
    board.

    A list subclass rather than a new return type, deliberately: every caller
    of every fetcher treats the result as a list and keeps doing so, `==`
    against a plain list still holds, and a fetcher that does not report the
    number returns an ordinary list whose board_total reads as None. "Not
    reported" and "reported as zero" therefore stay distinguishable, which
    matters because the whole value here is in telling zeros apart.
    """

    def __init__(self, jobs=(), board_total: "int | None" = None) -> None:
        super().__init__(jobs)
        self.board_total = board_total


def board_total_of(jobs) -> "int | None":
    """How many postings the board held, or None if the fetcher didn't say.

    A function rather than `getattr` at each call site so that "a plain list
    means unknown" is stated once."""
    return getattr(jobs, "board_total", None)
