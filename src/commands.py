# -*- coding: utf-8 -*-
"""
Telegram command processing: /add /remove /list. There's no separate
workflow for commands - this piggybacks on every run of check.yml, because
a dedicated cron every few minutes would, on its own, burn through the
entire Actions quota of a private repo (see the project discussion). The
consequence: response time for a command is up to one cron interval
(3 hours) + GitHub's usual scheduling delay.

v1: /add does NOT do automatic profiling - this is an explicit decision,
not a forgotten TODO. It only tells Orel that a manual profiling session
with career-site-profiler is needed.

Note: the Telegram message strings below (what gets sent back to the user)
are in Hebrew on purpose - that's product content for the user, who
converses in Hebrew, not a code comment. Code comments in this file are
in English per the project's comment convention.
"""

import json
import os
from pathlib import Path

import requests

from profiles import PROFILES_DIR
from notifier import send_message, escape_mdv2

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
        # Unrecognized commands are ignored silently - no need to flood
        # error messages for every typo

    _save_offset(max_update_id)
