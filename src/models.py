# -*- coding: utf-8 -*-
"""
The core data model of the project. Every fetcher - of any kind - returns
list[Job]. Diffing, deduplication, and "new job" detection always run on
Job.id, never on display text.
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
    # to_dict() as well - nothing about them ever reaches state/seen/*.json.
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

    def to_dict(self) -> dict:
        """Serialization for state/seen/<slug>.json. description and
        min_years_exp are deliberately NOT serialized: state means "every id
        ever seen", and a filter verdict is presentation, not identity -
        persisting it would tempt a future replay mechanism that this design
        explicitly rules out."""
        return {
            "id": self.id, "title": self.title, "location": self.location,
            "url": self.url, "company": self.company,
        }

    @staticmethod
    def from_dict(d: dict) -> "Job":
        return Job(id=d["id"], title=d["title"], location=d["location"],
                   url=d["url"], company=d["company"])
