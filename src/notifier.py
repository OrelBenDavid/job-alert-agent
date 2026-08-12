# -*- coding: utf-8 -*-
"""
Sending Telegram notifications. Every call to the Telegram API goes through
this module only - including MarkdownV2 escaping, which is a common source
of bugs (Telegram requires escaping characters like . - ( ) ! even in plain
text, not just in formatting syntax).
"""

import os
import re
import requests

from models import Job

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Characters that must be escaped in MarkdownV2, including inside plain
# text - not just the formatting syntax itself. Missing even one causes a
# silent 400 on the whole message.
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def escape_mdv2(text: str) -> str:
    """Escapes every MarkdownV2 special character. Used on all free text
    (job titles, company names) - never on the syntax itself
    (like [text](url))."""
    return re.sub(f"([{re.escape(_MDV2_SPECIAL)}])", r"\\\1", text)


def _get_credentials() -> tuple[str, str]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    return token, chat_id


def send_message(text: str, parse_mode: str = "MarkdownV2") -> None:
    """Sends a single message. Not swallowed silently: if Telegram returns
    an error, it propagates - better for the run to fail loudly than for
    an alert to vanish without a trace."""
    token, chat_id = _get_credentials()
    r = requests.post(
        TELEGRAM_API.format(token=token, method="sendMessage"),
        json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
              "disable_web_page_preview": True},
        timeout=15,
    )
    r.raise_for_status()


# Telegram rejects any message over 4096 characters outright, with a 400.
# A new-jobs alert must never be truncated the way /jobs is - every new job
# has to be delivered - so an oversized batch is SPLIT across messages
# instead. Measured against real data: a Mobileye batch breaches the limit at
# ~30 jobs once experience tags are included (~35 without them), which a
# company reworking its board can produce in a single 3-hour window.
TELEGRAM_MAX_CHARS = 4096

# Room reserved for the " \(12/34\)" part counter, which isn't known until
# after packing decides how many parts there are.
_PART_COUNTER_RESERVE = 12


def _job_block(job: Job, tags: dict) -> str:
    """One job's lines: the link, plus its filter tag if it has one.

    Kept together as a unit so packing can never split a job from its tag.
    display() is called only here - this is the single place in the whole
    project where "Title — Location" is ever constructed."""
    lines = [f"• [{escape_mdv2(job.display())}]({job.url})"]
    tag = tags.get(job.id)
    if tag:
        # Indented under its job rather than appended to the link text, so
        # the tag can never end up inside the clickable label.
        lines.append(f"   {escape_mdv2(tag)}")
    return "\n".join(lines)


def format_new_jobs_messages(company_name: str, jobs: list[Job],
                             tags: dict = None) -> list[str]:
    """Builds the grouped alert for one company, as one message per chunk.

    Returns a list because a big batch has to span several messages; the
    common case is a single-element list. Every job is present in exactly one
    chunk - nothing is dropped, which is the difference between this and
    format_job_list_message's "+N more" truncation.

    `tags` maps job.id -> a short label produced by whichever filter had
    something to say about that job. Keyed by id like everything else in this
    project, and optional: a job with no tag renders exactly as it did before
    the filter chain existed, which is what a run with every filter disabled
    produces."""
    tags = tags or {}
    header = f"🔔 *{escape_mdv2(company_name)}* — {len(jobs)} new jobs"
    budget = TELEGRAM_MAX_CHARS - len(header) - _PART_COUNTER_RESERVE - 2

    chunks: list[list[str]] = [[]]
    used = 0
    for job in jobs:
        block = _job_block(job, tags)
        if len(block) > budget:
            # Pathological (a title thousands of characters long). Truncate
            # rather than emit a message Telegram will certainly reject -
            # a rejected send costs the whole batch, a trimmed line costs
            # some text.
            block = block[:budget - 1] + "…"
        # +1 for the newline joining this block to the previous one.
        if chunks[-1] and used + 1 + len(block) > budget:
            chunks.append([])
            used = 0
        used += (1 if chunks[-1] else 0) + len(block)
        chunks[-1].append(block)

    total = len(chunks)
    messages = []
    for index, blocks in enumerate(chunks, start=1):
        # The counter is only added when there really is more than one part,
        # so the ordinary single-message case looks untouched.
        counter = f" \\({index}/{total}\\)" if total > 1 else ""
        messages.append(f"{header}{counter}\n\n" + "\n".join(blocks))
    return messages


# Telegram messages cap out at 4096 chars. A company with 100+ open roles
# would blow past that (or get rejected outright by the API) if every job
# were listed - so /jobs shows a bounded sample plus a link to the careers
# page for the rest, instead of paginating into several messages. 20 was
# chosen with real headroom, not just under the limit: Mobileye's actual
# job titles/links measured ~4060/4096 chars at a cap of 30 - too close
# for companies with longer titles or locations to stay safe at 30.
_MAX_JOBS_IN_LIST_REPLY = 20


def format_job_list_message(company_name: str, jobs: list[Job], careers_url: str) -> str:
    """Snapshot for the /jobs command - "here's what's open right now",
    visually distinct from format_new_jobs_messages ("here's what's new
    since last time"). Truncating with "+N more" is right HERE and wrong
    there: this is a snapshot the user asked for and can always re-request,
    while a new-jobs alert is the only time those postings are ever
    mentioned. Reuses display()/escape_mdv2 the same way, per the project
    convention that job-line formatting only ever happens in this module."""
    header = f"📋 *{escape_mdv2(company_name)}* — {len(jobs)} open jobs"
    if not jobs:
        return f"{header}\n\n_\\(No open Israel\\-relevant jobs right now\\.\\)_"

    shown = jobs[:_MAX_JOBS_IN_LIST_REPLY]
    lines = [header, ""]
    for j in shown:
        title_loc = escape_mdv2(j.display())
        lines.append(f"• [{title_loc}]({j.url})")

    remaining = len(jobs) - len(shown)
    if remaining > 0:
        lines.append("")
        lines.append(f"_\\+{remaining} more — see [the careers page]({careers_url})\\._")
    return "\n".join(lines)


def format_maintenance_alert(slug: str, message: str) -> str:
    """Maintenance alert - visually distinct from a jobs alert so the two
    are never confused."""
    return (f"⚠️ *Maintenance needed: {escape_mdv2(slug)}*\n\n"
           f"{escape_mdv2(message)}")


def notify_new_jobs(company_name: str, jobs: list[Job], tags: dict = None) -> None:
    """Sends one company's alert, across several messages if the batch is
    large. Deliberately NOT wrapped in a try/except here or in the caller -
    see the note in run.py on why a failed send must stay loud."""
    if not jobs:
        return
    for message in format_new_jobs_messages(company_name, jobs, tags):
        send_message(message)


def notify_maintenance(slug: str, message: str) -> None:
    send_message(format_maintenance_alert(slug, message))
