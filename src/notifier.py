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


def format_new_jobs_message(company_name: str, jobs: list[Job]) -> str:
    """Builds a grouped message for one company. display() is called only
    here - this is the single place in the whole project where
    "Title — Location" is ever constructed."""
    header = f"🔔 *{escape_mdv2(company_name)}* — {len(jobs)} new jobs"
    lines = [header, ""]
    for j in jobs:
        title_loc = escape_mdv2(j.display())
        lines.append(f"• [{title_loc}]({j.url})")
    return "\n".join(lines)


def format_maintenance_alert(slug: str, message: str) -> str:
    """Maintenance alert - visually distinct from a jobs alert so the two
    are never confused."""
    return (f"⚠️ *Maintenance needed: {escape_mdv2(slug)}*\n\n"
           f"{escape_mdv2(message)}")


def notify_new_jobs(company_name: str, jobs: list[Job]) -> None:
    if not jobs:
        return
    send_message(format_new_jobs_message(company_name, jobs))


def notify_maintenance(slug: str, message: str) -> None:
    send_message(format_maintenance_alert(slug, message))
