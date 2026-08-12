# -*- coding: utf-8 -*-
"""
The failure modes that only show up under scale or bad luck.

Every test here corresponds to a way the bot could go quiet, or go loud
forever, while every individual component still looked healthy. They are
grouped by what breaks rather than by module, because that is how they were
found: by asking "what happens to the OTHER 99 companies when this one
misbehaves?"
"""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import commands as commands_mod
import detail as detail_mod
import fetchers
import notifier as notifier_mod
import run as run_mod
import settings as settings_mod
import state as state_mod
import stats as stats_mod
from models import Job
from notifier import format_job_list_message, format_new_jobs_messages


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path / "seen")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "filters.json")
    monkeypatch.setattr(stats_mod, "STATS_PATH", tmp_path / "filter_stats.json")
    monkeypatch.setattr(commands_mod, "TELEGRAM_STATE_PATH",
                        tmp_path / "telegram.json")
    # The pacer would otherwise make every send test sleep for a second.
    monkeypatch.setattr(notifier_mod, "_MIN_SEND_INTERVAL", 0)
    yield tmp_path


def _job(job_id, url="https://example.com/j", title="Backend Developer"):
    return Job(id=job_id, title=title, location="Tel Aviv", url=url,
               company="acme")


# ---------------------------------------------------------------------------
# A single malformed job must not be able to silence the whole bot
# ---------------------------------------------------------------------------

def test_a_job_with_no_url_renders_as_text_not_an_empty_link():
    """The worst bug this suite covers, because it is permanent.

    A card whose link_selector missed produces url="". Rendered as "[label]()"
    that is an empty MarkdownV2 link destination, which Telegram rejects with
    a 400 - failing the company's whole batch, which fails the send, which
    means state is never committed, which means the same broken job is
    re-detected and re-sent next run, and the next. One malformed card would
    take every alert for every company down forever."""
    messages = format_new_jobs_messages("Acme", [_job("a", url="")])
    assert "]()" not in messages[0]
    assert "Backend Developer" in messages[0]


def test_a_url_containing_a_paren_is_escaped_inside_the_link():
    """An unescaped ")" closes the link early and leaves the tail as
    unescaped text - the same 400, by a different route."""
    messages = format_new_jobs_messages(
        "Acme", [_job("a", url="https://x.com/jobs/a(b)c")])
    assert "https://x.com/jobs/a(b\\)c" in messages[0]


def test_url_escaping_leaves_ordinary_link_characters_alone():
    """The other direction: escaping a URL the way free text is escaped would
    mangle the "-", "." and "_" that every real job link contains."""
    url = "https://careers.wix.com/position/oracle-f77c_829d.x?id=5"
    messages = format_new_jobs_messages("Acme", [_job("a", url=url)])
    assert f"({url})" in messages[0]


def test_the_jobs_command_reply_follows_the_same_rule():
    reply = format_job_list_message("Acme", [_job("a", url="")],
                                    "https://acme.com/careers")
    assert "]()" not in reply


# ---------------------------------------------------------------------------
# Telegram rate limiting - the likeliest trigger of everything above at 100
# companies, since every company sends to the same chat
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_a_429_is_retried_after_the_cooldown_telegram_asked_for(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    responses = [_Response(429, {"parameters": {"retry_after": 2}}),
                 _Response(200)]
    slept = []
    monkeypatch.setattr(notifier_mod.time, "sleep", slept.append)

    with patch.object(notifier_mod.requests, "post",
                      side_effect=lambda *a, **k: responses.pop(0)):
        notifier_mod.send_message("hi")

    assert slept == [2.0]        # honoured Telegram's own number
    assert not responses         # and the retry actually went out


def test_a_400_is_not_retried(monkeypatch):
    """Only throttling is retried. A rejection is a real failure and has to
    stay loud - retrying it would just fail three more times."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    calls = []

    def post(*args, **kwargs):
        calls.append(1)
        return _Response(400)

    with patch.object(notifier_mod.requests, "post", side_effect=post):
        with pytest.raises(RuntimeError):
            notifier_mod.send_message("hi")

    assert len(calls) == 1


def test_an_absurd_retry_after_is_not_honoured(monkeypatch):
    """A cooldown longer than the cap is not worth holding the run for -
    the next cron is three hours away anyway."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    with patch.object(notifier_mod.requests, "post",
                      return_value=_Response(429, {"parameters":
                                                   {"retry_after": 9999}})):
        with pytest.raises(RuntimeError):
            notifier_mod.send_message("hi")


# ---------------------------------------------------------------------------
# The Telegram command loop
# ---------------------------------------------------------------------------

def test_a_command_that_fails_still_advances_the_offset(monkeypatch):
    """Otherwise Telegram hands the same update back on every run for the 24
    hours it retains it, and a command that failed once fails identically
    every time. For /jobs that is an infinite retry costing a live fetch (and
    a browser launch) per run."""
    monkeypatch.setattr(commands_mod, "_fetch_updates",
                        lambda: [{"update_id": 7,
                                  "message": {"text": "/list"}}])

    def refuse(*args, **kwargs):
        raise RuntimeError("Telegram 400")

    monkeypatch.setattr(commands_mod, "send_message", refuse)
    commands_mod.process_commands()      # must not raise

    assert commands_mod._load_offset() == 7


def test_one_bad_command_does_not_block_the_ones_after_it(monkeypatch):
    monkeypatch.setattr(commands_mod, "_fetch_updates", lambda: [
        {"update_id": 1, "message": {"text": "/list"}},
        {"update_id": 2, "message": {"text": "/stats"}},
    ])
    sent = []

    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(commands_mod, "_handle_list", boom)
    monkeypatch.setattr(commands_mod, "_handle_stats", lambda: "the stats")
    monkeypatch.setattr(commands_mod, "send_message", sent.append)
    commands_mod.process_commands()

    assert sent == ["the stats"]
    assert commands_mod._load_offset() == 2


def test_a_corrupt_offset_file_does_not_kill_the_command_layer():
    commands_mod.TELEGRAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    commands_mod.TELEGRAM_STATE_PATH.write_text("{trunc", encoding="utf-8")
    assert commands_mod._load_offset() == 0


def test_the_offset_is_written_atomically():
    commands_mod._save_offset(42)
    assert commands_mod._load_offset() == 42
    assert not list(commands_mod.TELEGRAM_STATE_PATH.parent.glob("*.tmp"))


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------

def _profile(zero_is_plausible=False, expected_min_jobs=0):
    return SimpleNamespace(zero_is_plausible=zero_is_plausible,
                           expected_min_jobs=expected_min_jobs)


def test_a_corrupt_state_file_raises_a_typed_error_not_a_json_error():
    """It used to escape as a bare JSONDecodeError from a loop that runs
    BEFORE any company is fetched - so one damaged file took the entire run
    down, every company, no alerts."""
    state_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (state_mod.STATE_DIR / "acme.json").write_text("{oh no", encoding="utf-8")

    with pytest.raises(state_mod.StateUnreadable):
        state_mod.load_state("acme")


def test_a_corrupt_state_file_never_reads_as_never_seeded():
    """The distinction that matters: "never seeded" invites a re-seed, and a
    re-seed on a company that HAS state silently marks every currently-open
    posting as already known."""
    state_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (state_mod.STATE_DIR / "acme.json").write_text('["not", "an", "object"]',
                                                   encoding="utf-8")
    with pytest.raises(state_mod.StateUnreadable):
        state_mod.load_state("acme")


def test_a_state_file_missing_keys_is_filled_in_rather_than_raising():
    """A hand-edited or older file used to raise KeyError from inside the
    health gate - mid-run, after the fetch was already paid for."""
    state_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (state_mod.STATE_DIR / "acme.json").write_text(
        json.dumps({"last_success": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8")

    loaded = state_mod.load_state("acme")
    assert loaded["last_count"] == 0 and loaded["jobs"] == {}


# ---------------------------------------------------------------------------
# The health gate's second threshold
# ---------------------------------------------------------------------------

def test_a_partial_collapse_below_the_floor_is_caught():
    """What broken pagination looks like: page 1 still parses, pages 2..N
    stop coming. The count stays well above zero, so only expected_min_jobs
    can see it - and that field used to be read by nothing at all."""
    jobs = [_job(f"id{i}") for i in range(30)]
    state_mod.seed_company("acme", jobs)

    result = state_mod.process_company("acme", jobs[:4],
                                       _profile(expected_min_jobs=20))
    assert result.status == "empty_suspicious"
    # and the 26 ids it would otherwise have dropped are still on disk
    assert len(state_mod.load_state("acme")["jobs"]) == 30


def test_ordinary_churn_below_the_previous_count_is_not_a_collapse():
    jobs = [_job(f"id{i}") for i in range(30)]
    state_mod.seed_company("acme", jobs)

    result = state_mod.process_company("acme", jobs[:25],
                                       _profile(expected_min_jobs=20))
    assert result.status == "ok"


def test_a_company_that_was_already_below_its_floor_does_not_re_alert():
    """`floor <= previous` in the gate: a floor set too high for a company
    that genuinely shrank produces one hit, not a permanent one."""
    jobs = [_job(f"id{i}") for i in range(5)]
    state_mod.seed_company("acme", jobs)          # last_count = 5

    result = state_mod.process_company("acme", jobs[:4],
                                       _profile(expected_min_jobs=20))
    assert result.status == "ok"


def test_zero_is_plausible_disables_the_floor_too():
    jobs = [_job(f"id{i}") for i in range(30)]
    state_mod.seed_company("acme", jobs)
    result = state_mod.process_company(
        "acme", [], _profile(zero_is_plausible=True, expected_min_jobs=20))
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# Time budgets
# ---------------------------------------------------------------------------

class _FakeProfile:
    def __init__(self, slug, fetch_type="api"):
        self.slug = slug
        self.name = slug.title()
        self.fetch_type = fetch_type


def test_the_fetch_deadline_skips_companies_rather_than_failing_them():
    """A workflow timeout kills the job before the commit step, so ALL of a
    run's state writes are thrown away and the next run walks into the same
    wall. Finishing early with some companies skipped is the better failure -
    and a skipped company must not look like a broken one."""
    profiles = [_FakeProfile(f"c{i}") for i in range(6)]

    def slow(profile):
        time.sleep(0.15)
        return []

    with patch.dict("os.environ", {"JOB_ALERT_NETWORK_WORKERS": "1"}), \
         patch.object(fetchers, "fetch_jobs", side_effect=slow):
        outcomes = fetchers.fetch_all(profiles,
                                      deadline=time.monotonic() + 0.2)

    assert len(outcomes) < len(profiles)          # some were dropped
    assert all(o.ok for o in outcomes.values())   # and none as failures


def test_no_deadline_still_fetches_everything():
    profiles = [_FakeProfile(f"c{i}") for i in range(4)]
    with patch.object(fetchers, "fetch_jobs", side_effect=lambda p: []):
        outcomes = fetchers.fetch_all(profiles)
    assert len(outcomes) == 4


def test_a_company_skipped_by_the_budget_is_not_counted_as_a_failure(
        monkeypatch, tmp_path):
    """run.py must tell "we never tried" apart from "we tried and it broke":
    only the second bumps the failure counter toward a maintenance alert."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "acme.json").write_text(json.dumps({
        "schema_version": 3, "slug": "acme", "name": "Acme", "enabled": True,
        "careers_url": "https://jobs.lever.co/acme", "fetch_type": "api",
        "israel_filter": {"method": "post_fetch"},
        "api": {"platform": "lever", "endpoint": "https://api.lever.co/x",
                "fields": {"id": "id", "title": "text",
                           "location": "categories.location",
                           "url": "hostedUrl"}},
        "health": {"expected_min_jobs": 0}, "verified_on": "2026-08-12",
    }), encoding="utf-8")

    import profiles as profiles_mod
    monkeypatch.setattr(run_mod, "load_enabled",
                        lambda: profiles_mod.load_enabled(profiles_dir))
    monkeypatch.setattr(run_mod, "process_commands", lambda: None)
    monkeypatch.setattr(run_mod, "fetch_all",
                        lambda profiles, deadline=None: {})   # nothing started
    maintenance = []
    monkeypatch.setattr(run_mod, "notify_maintenance",
                        lambda slug, msg: maintenance.append(slug))

    state_mod.seed_company("acme", [])
    run_mod._run_normal()

    assert maintenance == []
    assert state_mod.load_state("acme")["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# The detail budget is a RUN budget
# ---------------------------------------------------------------------------

def test_the_detail_budget_is_shared_across_companies(monkeypatch):
    """It was described as per-run and enforced per company, because each
    call restarted its own count. At three companies that is invisible; at a
    hundred it is 40 requests versus 4000, which no workflow timeout fits."""
    monkeypatch.setattr(detail_mod, "MAX_DETAIL_FETCHES_PER_RUN", 5)
    budget = detail_mod.DetailBudget()
    cfg = {"method": "html", "content_selector": "#d",
           "verified_on_job_url": "https://x/1"}
    profile = SimpleNamespace(slug="acme", detail_fetch=cfg)

    with patch.object(detail_mod.requests, "Session") as session_cls:
        response = SimpleNamespace(text="<div id='d'>hi</div>",
                                   raise_for_status=lambda: None)
        session_cls.return_value.get.return_value = response

        for _ in range(3):       # three companies, four jobs each
            detail_mod.enrich([_job(str(i)) for i in range(4)], profile, budget)

        assert session_cls.return_value.get.call_count == 5


def test_jobs_past_the_budget_are_still_returned_undetermined():
    """The overflow fails open like every other detail miss."""
    budget = detail_mod.DetailBudget(remaining=0)
    cfg = {"method": "html", "content_selector": "#d",
           "verified_on_job_url": "https://x/1"}
    profile = SimpleNamespace(slug="acme", detail_fetch=cfg)

    jobs = [_job(str(i)) for i in range(4)]
    out = detail_mod.enrich(jobs, profile, budget)

    assert len(out) == 4
    assert all(job.description is None for job in out)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def test_seeding_skips_companies_that_already_have_state(monkeypatch, tmp_path):
    """The case that matters when adding companies in batches: seeding an
    already-seeded company resets every first_seen to the seed timestamp, and
    costs a full fetch to do it. Only the new ones should be touched."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    for slug in ("old", "new"):
        (profiles_dir / f"{slug}.json").write_text(json.dumps({
            "schema_version": 3, "slug": slug, "name": slug, "enabled": True,
            "careers_url": f"https://jobs.lever.co/{slug}", "fetch_type": "api",
            "israel_filter": {"method": "post_fetch"},
            "api": {"platform": "lever", "endpoint": "https://api.lever.co/x",
                    "fields": {"id": "id", "title": "text",
                               "location": "categories.location",
                               "url": "hostedUrl"}},
            "health": {"expected_min_jobs": 0}, "verified_on": "2026-08-12",
        }), encoding="utf-8")

    import profiles as profiles_mod
    from fetchers import FetchOutcome
    monkeypatch.setattr(run_mod, "load_enabled",
                        lambda: profiles_mod.load_enabled(profiles_dir))

    fetched = []

    def fake_fetch(profiles, deadline=None):
        fetched.extend(p.slug for p in profiles)
        return {p.slug: FetchOutcome(p.slug, [_job("j1")], None, 0.0)
                for p in profiles}

    monkeypatch.setattr(run_mod, "fetch_all", fake_fetch)

    state_mod.seed_company("old", [_job("kept")])
    first_seen = state_mod.load_state("old")["jobs"]["kept"]["first_seen"]

    run_mod._run_seed()

    assert fetched == ["new"]          # the seeded one cost no fetch at all
    assert state_mod.load_state("old")["jobs"]["kept"]["first_seen"] == first_seen
    assert set(state_mod.load_state("new")["jobs"]) == {"j1"}


def test_seeding_never_overwrites_an_unreadable_state_file(tmp_path):
    """Re-seeding a company whose ids are on disk but unparseable would mark
    every currently-open posting as already known - and those postings would
    then never be alerted. The fix is git history, not this command."""
    state_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (state_mod.STATE_DIR / "acme.json").write_text("{broken", encoding="utf-8")
    assert run_mod._needs_seed("acme") is False
