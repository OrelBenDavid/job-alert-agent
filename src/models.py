# -*- coding: utf-8 -*-
"""
The core data model of the project. Every fetcher - of any kind - returns
list[Job]. Diffing, deduplication, and "new job" detection always run on
Job.id, never on display text.
"""

from dataclasses import dataclass


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

    def display(self) -> str:
        """The agreed display format only. Built here, never stored in
        state, and never used as a comparison key - otherwise a cosmetic
        change on the company's side (a space becoming a hyphen, "(Hybrid)"
        appended to a title) would look like a brand-new job."""
        return f"{self.title} — {self.location}"

    def to_dict(self) -> dict:
        """Serialization for state/seen/<slug>.json."""
        return {
            "id": self.id, "title": self.title, "location": self.location,
            "url": self.url, "company": self.company,
        }

    @staticmethod
    def from_dict(d: dict) -> "Job":
        return Job(id=d["id"], title=d["title"], location=d["location"],
                   url=d["url"], company=d["company"])
