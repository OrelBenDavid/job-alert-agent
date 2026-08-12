# -*- coding: utf-8 -*-
"""
Tests for alert formatting - specifically the 4096-character ceiling.

Telegram rejects an oversized message outright with a 400. Because state is
written before notification, a rejected send is the one place in the whole
pipeline where jobs could go missing, so the packing here is load-bearing
rather than cosmetic.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from models import Job
from notifier import TELEGRAM_MAX_CHARS, format_new_jobs_messages


def _job(i, title="Backend Developer", location="Tel Aviv"):
    return Job(id=str(i), title=title, location=location,
               url=f"https://example.com/jobs/{i}", company="acme")


_TAG = "⚠️ לא צוינה דרישת ניסיון"


def test_a_small_batch_is_a_single_message():
    messages = format_new_jobs_messages("Acme", [_job(1), _job(2)])
    assert len(messages) == 1
    assert messages[0].startswith("🔔 *Acme* — 2 new jobs\n\n")
    assert "(1/1)" not in messages[0]      # no counter when there's one part


def test_every_message_stays_under_the_limit():
    jobs = [_job(i) for i in range(200)]
    messages = format_new_jobs_messages("Acme", jobs, {j.id: _TAG for j in jobs})
    assert len(messages) > 1
    for message in messages:
        assert len(message) <= TELEGRAM_MAX_CHARS


@pytest.mark.parametrize("count", [1, 25, 29, 30, 31, 40, 122, 300])
def test_no_job_is_ever_dropped_or_duplicated(count):
    """The difference between this and /jobs' "+N more": a new-jobs alert is
    the only time these postings are ever mentioned."""
    jobs = [_job(i) for i in range(count)]
    combined = "\n".join(
        format_new_jobs_messages("Acme", jobs, {j.id: _TAG for j in jobs}))
    for job in jobs:
        # The full rendered link, not the bare URL - ".../jobs/1" is a
        # substring of ".../jobs/10" and would count matches that aren't it.
        assert combined.count(f"]({job.url})") == 1


def test_multi_part_messages_are_numbered():
    jobs = [_job(i) for i in range(120)]
    messages = format_new_jobs_messages("Acme", jobs)
    total = len(messages)
    assert total > 1
    for index, message in enumerate(messages, start=1):
        assert f"\\({index}/{total}\\)" in message


def test_a_job_is_never_split_from_its_tag():
    """A tag stranded at the top of the next message would read as belonging
    to the wrong job."""
    jobs = [_job(i) for i in range(120)]
    messages = format_new_jobs_messages("Acme", jobs, {j.id: _TAG for j in jobs})
    for message in messages:
        body = message.split("\n\n", 1)[1]
        # Every tag line must be preceded by its job's link line.
        lines = body.split("\n")
        for position, line in enumerate(lines):
            if line.strip().startswith("⚠️"):
                assert lines[position - 1].startswith("• [")


def test_tags_are_escaped_for_markdownv2():
    """A fractional threshold puts a '.' in the tag, which Telegram treats as
    a special character and rejects unescaped."""
    job = _job(1)
    messages = format_new_jobs_messages("Acme", [job],
                                        {job.id: "✅ ניסיון: עד 2.5 שנים"})
    assert "2\\.5" in messages[0]


def test_an_absurdly_long_title_is_truncated_rather_than_sent_oversized():
    """Pathological, but a rejected send costs the whole batch while a
    trimmed line costs some text."""
    jobs = [_job(1, title="X" * 6000), _job(2)]
    messages = format_new_jobs_messages("Acme", jobs)
    for message in messages:
        assert len(message) <= TELEGRAM_MAX_CHARS
    assert "…" in messages[0]
    assert any(_job(2).url in m for m in messages)   # the next job survived


def test_untagged_jobs_render_exactly_as_before_the_filter_existed():
    messages = format_new_jobs_messages("Acme", [_job(1)])
    assert messages[0] == (
        "🔔 *Acme* — 1 new jobs\n\n"
        "• [Backend Developer — Tel Aviv](https://example.com/jobs/1)")
