# -*- coding: utf-8 -*-
"""
Telegram command processing: /add /remove /list /jobs /filter /minexp
/stats. There's no separate
workflow for commands - this piggybacks on every run of check.yml, because
a dedicated cron every few minutes would, on its own, burn through the
entire Actions quota of a private repo (see the project discussion). The
consequence: response time for a command is up to one cron interval
(3 hours) + GitHub's usual scheduling delay.

v1: /add does NOT do automatic profiling - this is an explicit decision,
not a forgotten TODO. It only tells Orel that a manual profiling session
with career-site-profiler is needed.

/jobs <slug> is a live fetch, not a state read - it calls the same
fetch_jobs() the main check loop uses, so it always reflects the site
right now rather than whatever was last seen. That also means it costs
one extra live fetch per invocation (and, for a playwright profile, one
extra browser launch) - acceptable since commands are infrequent.

Note: the Telegram message strings below (what gets sent back to the user)
are in Hebrew on purpose - that's product content for the user, who
converses in Hebrew, not a code comment. Code comments in this file are
in English per the project's comment convention.
"""

import json
import os
from pathlib import Path

import requests

from profiles import PROFILES_DIR, load_profile, ProfileError
from fetchers import fetch_jobs
from notifier import send_message, escape_mdv2, format_job_list_message
from settings import load_settings, update_filter
from stats import load_stats, COUNTER_NAMES

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "telegram.json"


def _load_offset() -> int:
    if not TELEGRAM_STATE_PATH.exists():
        return 0
    return json.loads(TELEGRAM_STATE_PATH.read_text(encoding="utf-8")).get("offset", 0)


def _save_offset(offset: int) -> None:
    TELEGRAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TELEGRAM_STATE_PATH.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def _fetch_updates() -> list[dict]:
    """Pulls new messages from Telegram based on the saved offset. offset+1
    tells Telegram the previous messages were handled and won't be
    returned again."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    offset = _load_offset()
    r = requests.get(
        TELEGRAM_API.format(token=token, method="getUpdates"),
        params={"offset": offset + 1 if offset else 0, "timeout": 0},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("result", [])


def _list_profile_paths() -> list[Path]:
    return sorted(PROFILES_DIR.glob("*.json"))


def _handle_list() -> str:
    lines = ["📋 *חברות במעקב:*", ""]
    for path in _list_profile_paths():
        data = json.loads(path.read_text(encoding="utf-8"))
        mark = "✅" if data.get("enabled") else "⏸️"
        lines.append(f"{mark} {escape_mdv2(data.get('name', data['slug']))}")
    if len(lines) == 2:
        lines.append("_(אין חברות עדיין)_")
    return "\n".join(lines)


def _handle_remove(slug: str) -> str:
    """Disables a company (enabled=false) - doesn't delete the file or its
    state, so it can be brought back without losing history."""
    path = PROFILES_DIR / f"{slug}.json"
    if not path.exists():
        return f"❌ לא נמצא פרופיל בשם {escape_mdv2(slug)}"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["enabled"] = False
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"⏸️ {escape_mdv2(data.get('name', slug))} הוסרה ממעקב \\(ניתן להחזיר\\)"


def _handle_add(company_name: str) -> str:
    """v1: no automatic profiling from GitHub Actions. The command only
    points to running a manual career-site-profiler session - this is an
    explicit product decision."""
    return (f"🔧 כדי להוסיף את {escape_mdv2(company_name)} צריך סשן פרופיילינג "
           f"ידני עם הסקיל career\\-site\\-profiler\\. פתח שיחה חדשה ובקש "
           f"להוסיף את החברה\\.")


def _handle_jobs(slug: str) -> str:
    """On-demand snapshot of a company's currently open (Israel-relevant)
    jobs, straight from the source - unlike /list, which only shows
    tracked company names from local files."""
    path = PROFILES_DIR / f"{slug}.json"
    if not path.exists():
        return f"❌ לא נמצא פרופיל בשם {escape_mdv2(slug)}"

    try:
        profile = load_profile(path)
    except ProfileError as e:
        return f"❌ פרופיל שגוי: {escape_mdv2(str(e))}"

    try:
        jobs = fetch_jobs(profile)
    except Exception as e:
        return (f"❌ שגיאה בשליפת משרות עבור {escape_mdv2(profile.name)}: "
               f"{escape_mdv2(str(e))}")

    return format_job_list_message(profile.name, jobs, profile.careers_url)


def _handle_filter(arg: str) -> str:
    """/filter                    - state of every filter
       /filter <name> on|off      - toggle one

    The toggle writes to state/filters.json, which check.yml already commits
    back to the repo - that's what makes it changeable from a phone with no
    commit and no workflow edit."""
    settings = load_settings()

    if not arg:
        lines = ["🎛️ *מסננים:*", ""]
        for name, cfg in settings.items():
            mark = "✅ פעיל" if cfg.get("enabled") else "⏸️ כבוי"
            lines.append(f"• {escape_mdv2(name)} — {mark}")
            if name == "experience":
                strict = "כן" if cfg.get("strict") else "לא"
                threshold = escape_mdv2("%g" % cfg["max_years"])
                lines.append(f"   סף: {threshold} שנים, strict: {strict}")
        return "\n".join(lines)

    parts = arg.split()
    name = parts[0].lower()
    if name not in settings:
        return f"❌ אין מסנן בשם {escape_mdv2(name)}"
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        # No code spans anywhere in these replies: inside a MarkdownV2 code
        # entity only ` and \ are escapable, so the ordinary escaping used
        # everywhere else would render as literal backslashes.
        return "❌ שימוש: /filter experience on\\|off"

    enabled = parts[1].lower() == "on"
    update_filter(name, enabled=enabled)
    state_text = "הופעל" if enabled else "כובה"
    note = ("" if enabled else
            "\n\n_משרות שנחסמו בזמן שהמסנן היה פעיל לא יישלחו רטרואקטיבית\\._")
    return f"🎛️ מסנן {escape_mdv2(name)} {state_text}\\.{note}"


def _handle_minexp(arg: str) -> str:
    """/minexp <N>              - the threshold, in years
       /minexp strict on|off    - make "undetermined" reject instead of pass"""
    if not arg:
        cfg = load_settings()["experience"]
        strict = "כן" if cfg.get("strict") else "לא"
        threshold = escape_mdv2("%g" % cfg["max_years"])
        return f"📏 סף ניסיון נוכחי: {threshold} שנים\nstrict: {strict}"

    parts = arg.split()
    if parts[0].lower() == "strict":
        if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
            return "❌ שימוש: /minexp strict on\\|off"
        strict = parts[1].lower() == "on"
        update_filter("experience", strict=strict)
        if strict:
            return ("🔒 strict הופעל — משרה שלא צוין בה ניסיון תיחסם\\. "
                    "שים לב: זה הופך את ברירת המחדל של fail\\-open\\.")
        return "🔓 strict כובה — משרה שלא צוין בה ניסיון תישלח \\(עם דגל\\)\\."

    try:
        max_years = float(parts[0])
    except ValueError:
        return "❌ שימוש: /minexp 1 — או — /minexp strict on\\|off"
    if max_years < 0:
        return "❌ הסף חייב להיות 0 או יותר"

    update_filter("experience", max_years=max_years)
    threshold = escape_mdv2("%g" % max_years)
    return (f"📏 סף ניסיון עודכן ל\\-{threshold} שנים\\. "
            "משרות שדורשות יותר מזה ייחסמו\\.")


# Hebrew labels for the five counters, in the order they're worth reading.
_STAT_LABELS = [
    ("rejected_by_title", "נחסמו לפי כותרת"),
    ("rejected_with_number", "נחסמו \\(מספר שנים\\)"),
    ("passed_with_number", "עברו \\(מספר שנים\\)"),
    ("undetermined_signals", "לא צוין מספר, סימני ותק"),
    ("undetermined", "לא צוינה דרישה"),
]


def _format_counters(counters: dict) -> list[str]:
    lines = []
    for key, label in _STAT_LABELS:
        lines.append(f"   {label}: {counters.get(key, 0)}")
    return lines


def _handle_stats() -> str:
    """The counters that answer the one question the design can't answer from
    the outside: is the filter actually working, or only appearing to?

    The ~70%-state-no-number figure it was built on is US aggregate data and
    may not hold for Israeli tech postings."""
    stored = load_stats()
    if not stored.get("last_run"):
        return "📊 אין עדיין נתוני סינון \\(לא רצה עדיין ריצה עם משרות חדשות\\)\\."

    lines = ["📊 *סטטיסטיקת סינון*", ""]
    for filter_name, counters in stored["last_run"].items():
        total = sum(counters.get(k, 0) for k in COUNTER_NAMES)
        lines.append(f"*{escape_mdv2(filter_name)}* — ריצה אחרונה \\({total} משרות\\):")
        lines.extend(_format_counters(counters))

    totals = stored.get("totals") or {}
    if totals:
        lines.append("")
        for filter_name, counters in totals.items():
            total = sum(counters.get(k, 0) for k in COUNTER_NAMES)
            lines.append(f"*{escape_mdv2(filter_name)}* — מצטבר \\({total} משרות\\):")
            lines.extend(_format_counters(counters))

    return "\n".join(lines)


def process_commands() -> None:
    """Called at the start of every run.py invocation, before the job check
    itself."""
    updates = _fetch_updates()
    if not updates:
        return

    max_update_id = _load_offset()
    for update in updates:
        max_update_id = max(max_update_id, update["update_id"])
        text = (update.get("message") or {}).get("text", "").strip()
        if not text.startswith("/"):
            continue

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/list":
            send_message(_handle_list())
        elif cmd == "/remove" and arg:
            send_message(_handle_remove(arg))
        elif cmd == "/add" and arg:
            send_message(_handle_add(arg))
        elif cmd == "/jobs" and arg:
            send_message(_handle_jobs(arg))
        elif cmd == "/filter":
            send_message(_handle_filter(arg))     # no arg = show current state
        elif cmd == "/minexp":
            send_message(_handle_minexp(arg))
        elif cmd == "/stats":
            send_message(_handle_stats())
        # Unrecognized commands are ignored silently - no need to flood
        # error messages for every typo

    _save_offset(max_update_id)
