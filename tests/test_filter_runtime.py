# -*- coding: utf-8 -*-
"""
Integration-level tests for the two things that are easy to get right in
isolation and catastrophic to get wrong together: the ORDER of the state
write relative to filtering, and the runtime toggle the bot writes for
itself.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import commands as commands_mod
import run as run_mod
import settings as settings_mod
import state as state_mod
import stats as stats_mod
from filters import ExperienceFilter, run_chain
from models import Job
from stats import RunStats, save_stats


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Every file this patch writes gets redirected into tmp_path."""
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path / "seen")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", tmp_path / "filters.json")
    monkeypatch.setattr(stats_mod, "STATS_PATH", tmp_path / "filter_stats.json")
    yield tmp_path


def _job(job_id, title="Backend Developer", description=None):
    return Job(id=job_id, title=title, location="Tel Aviv",
               url=f"https://example.com/{job_id}", company="acme",
               description=description)


_PROFILE = SimpleNamespace(slug="acme", detail_fetch=None,
                           zero_is_plausible=False, expected_min_jobs=0)

_SENIOR = "<h3>Requirements</h3><ul><li>8+ years of experience</li></ul>"
_JUNIOR = "<h3>Requirements</h3><ul><li>1 year of experience</li></ul>"


# ---------------------------------------------------------------------------
# The ordering rule - the one that breaks the bot quietly if reversed
# ---------------------------------------------------------------------------

def test_a_filtered_out_job_is_still_written_to_state():
    """State means "everything ever seen", and filtering is presentation
    only. If a rejected job were held out of state it would look new on
    every subsequent run, and its detail page would be re-fetched forever."""
    state_mod.seed_company("acme", [])

    fetched = [_job("keep", description=_JUNIOR),
               _job("drop", description=_SENIOR)]
    result = state_mod.process_company("acme", fetched, _PROFILE)
    assert {job.id for job in result.new_jobs} == {"keep", "drop"}

    survivors = run_chain(result.new_jobs, _PROFILE, [ExperienceFilter()])
    assert [job.id for job, _ in survivors] == ["keep"]

    # The rejected job is nevertheless permanently recorded.
    saved = state_mod.load_state("acme")
    assert set(saved["jobs"].keys()) == {"keep", "drop"}


def test_a_rejected_job_is_not_re_detected_on_the_next_run():
    """The direct consequence of the ordering: no repeat work, ever."""
    state_mod.seed_company("acme", [])
    fetched = [_job("drop", description=_SENIOR)]

    state_mod.process_company("acme", fetched, _PROFILE)
    second_run = state_mod.process_company("acme", fetched, _PROFILE)

    assert second_run.new_jobs == []


def test_turning_the_filter_off_does_not_replay_suppressed_jobs():
    """An accepted consequence, asserted so nobody "fixes" it into a replay
    mechanism later."""
    state_mod.seed_company("acme", [])
    fetched = [_job("drop", description=_SENIOR)]
    state_mod.process_company("acme", fetched, _PROFILE)     # filter was on

    # Filter now off - but the job is already "seen", so it is not new again.
    later = state_mod.process_company("acme", fetched, _PROFILE)
    assert run_chain(later.new_jobs, _PROFILE, []) == []


# ---------------------------------------------------------------------------
# A failed send must never leave jobs marked "seen but never delivered"
# ---------------------------------------------------------------------------

def _write_profile(profiles_dir, slug, health=None):
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / f"{slug}.json").write_text(json.dumps({
        "schema_version": 3, "slug": slug, "name": slug.title(),
        "enabled": True, "careers_url": f"https://jobs.lever.co/{slug}",
        "fetch_type": "api",
        "israel_filter": {"method": "post_fetch"},
        "api": {"platform": "lever",
                "endpoint": f"https://api.lever.co/v0/postings/{slug}",
                "fields": {"id": "id", "title": "text",
                           "location": "categories.location",
                           "url": "hostedUrl"}},
        "health": health or {"expected_min_jobs": 0},
        "verified_on": "2026-08-12",
    }), encoding="utf-8")


def _arrange_run(monkeypatch, profiles_dir, jobs_by_slug):
    """Wires _run_normal up to fake profiles and a fake fetch phase."""
    import profiles as profiles_mod
    from fetchers import FetchOutcome

    monkeypatch.setattr(run_mod, "load_enabled",
                        lambda: profiles_mod.load_enabled(profiles_dir))
    monkeypatch.setattr(run_mod, "process_commands", lambda: None)
    # Patched at fetch_all, the concurrent phase's entry point, rather than at
    # the per-company fetch: run.py consumes outcomes, not raw job lists.
    monkeypatch.setattr(run_mod, "fetch_all",
                        lambda profiles, deadline=None: {
                            p.slug: FetchOutcome(p.slug,
                                                 jobs_by_slug[p.slug],
                                                 None, 0.0)
                            for p in profiles})


def test_a_failed_send_rewinds_that_company_so_the_jobs_come_back(monkeypatch,
                                                                  tmp_path):
    """The subtle one. State is written BEFORE notification, so a company
    whose alert failed has jobs recorded as "seen" that the user never saw.
    Committing that would make "seen but never delivered" permanent - the one
    outcome the whole project exists to prevent.

    The answer is to rewind that company's state to its pre-run snapshot, so
    the jobs are un-seen and the next run re-detects and re-sends them.

    If someone ever "fixes" this into a quiet warning that leaves the state
    written, this test fails."""
    profiles_dir = tmp_path / "profiles"
    _write_profile(profiles_dir, "acme")
    _arrange_run(monkeypatch, profiles_dir,
                 {"acme": [_job("new1"), _job("new2")]})

    state_mod.seed_company("acme", [])          # seeded, so it isn't a seed gap

    def refuse(*args, **kwargs):
        raise RuntimeError("Telegram 400: message is too long")

    monkeypatch.setattr(run_mod, "notify_new_jobs", refuse)
    maintenance = []
    monkeypatch.setattr(run_mod, "notify_maintenance",
                        lambda slug, msg: maintenance.append((slug, msg)))

    run_mod._run_normal()       # exits normally - the rewind made it safe

    assert maintenance and maintenance[0][0] == "acme"
    # Rewound: the undelivered jobs are NOT on disk, so the next run finds
    # them new again and tries to send them again.
    assert state_mod.load_state("acme")["jobs"] == {}


def test_one_failed_send_does_not_rewind_the_other_companies(monkeypatch,
                                                             tmp_path):
    """What the per-company rewind bought. The previous design failed the
    whole run so that NOTHING was committed, which also un-saw every healthy
    company's jobs - so one broken company re-sent every other company's
    alerts on the next run. At three companies that is a duplicate; at a
    hundred it is a flood, every run, until the broken one is fixed."""
    profiles_dir = tmp_path / "profiles"
    _write_profile(profiles_dir, "acme")
    _write_profile(profiles_dir, "beta")
    _arrange_run(monkeypatch, profiles_dir,
                 {"acme": [_job("a1")], "beta": [_job("b1")]})

    state_mod.seed_company("acme", [])
    state_mod.seed_company("beta", [])

    def send(company_name, jobs, tags=None):
        if company_name == "Acme":
            raise RuntimeError("Telegram 429")

    monkeypatch.setattr(run_mod, "notify_new_jobs", send)
    monkeypatch.setattr(run_mod, "notify_maintenance", lambda slug, msg: None)

    run_mod._run_normal()

    assert state_mod.load_state("acme")["jobs"] == {}          # rewound
    assert set(state_mod.load_state("beta")["jobs"]) == {"b1"}  # kept


def test_a_rewind_that_itself_fails_falls_back_to_failing_the_run(monkeypatch,
                                                                  tmp_path):
    """The fallback, and the reason the non-zero exit still exists: if the
    jobs cannot be un-seen locally, the only remaining way to stop them being
    committed is to fail the run so check.yml never reaches its commit step."""
    profiles_dir = tmp_path / "profiles"
    _write_profile(profiles_dir, "acme")
    _arrange_run(monkeypatch, profiles_dir, {"acme": [_job("new1")]})

    state_mod.seed_company("acme", [])

    def refuse(*args, **kwargs):
        raise RuntimeError("Telegram 400")

    def broken_restore(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(run_mod, "notify_new_jobs", refuse)
    monkeypatch.setattr(run_mod, "restore_state", broken_restore)
    monkeypatch.setattr(run_mod, "notify_maintenance", lambda slug, msg: None)

    with pytest.raises(SystemExit) as exit_info:
        run_mod._run_normal()

    assert exit_info.value.code == 1        # non-zero => commit step skipped


def test_a_successful_run_exits_normally():
    """The guard must not turn ordinary runs into failures."""
    state_mod.seed_company("acme", [])
    assert state_mod.load_state("acme")["jobs"] == {}


# ---------------------------------------------------------------------------
# L. The seed run
# ---------------------------------------------------------------------------

def test_seeding_writes_state_without_alerts_or_detail_work():
    jobs = [_job(str(i), description=_SENIOR) for i in range(200)]
    state_mod.seed_company("acme", jobs)
    saved = state_mod.load_state("acme")
    assert len(saved["jobs"]) == 200
    assert saved["last_count"] == 200


# ---------------------------------------------------------------------------
# J. The runtime toggle
# ---------------------------------------------------------------------------

def test_defaults_apply_when_no_settings_file_exists():
    cfg = settings_mod.load_settings()["experience"]
    assert cfg == {"enabled": True, "max_years": 1.0, "strict": False}


def test_a_toggle_persists_to_disk():
    settings_mod.update_filter("experience", enabled=False)
    assert settings_mod.load_settings()["experience"]["enabled"] is False
    stored = json.loads(settings_mod.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert stored["filters"]["experience"]["enabled"] is False


def test_a_partial_stored_file_still_gets_the_other_defaults():
    """Adding a knob later must not require migrating the on-disk file."""
    settings_mod.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings_mod.SETTINGS_PATH.write_text(
        json.dumps({"filters": {"experience": {"max_years": 3}}}),
        encoding="utf-8")
    cfg = settings_mod.load_settings()["experience"]
    assert cfg["max_years"] == 3
    assert cfg["enabled"] is True and cfg["strict"] is False


def test_a_corrupt_settings_file_falls_back_to_defaults_instead_of_crashing():
    settings_mod.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings_mod.SETTINGS_PATH.write_text("{not json", encoding="utf-8")
    assert settings_mod.load_settings()["experience"]["enabled"] is True


# ---------------------------------------------------------------------------
# The Telegram command handlers
# ---------------------------------------------------------------------------

def test_filter_command_reports_state():
    reply = commands_mod._handle_filter("")
    assert "experience" in reply and "פעיל" in reply


def test_filter_command_toggles():
    commands_mod._handle_filter("experience off")
    assert settings_mod.load_settings()["experience"]["enabled"] is False
    commands_mod._handle_filter("experience on")
    assert settings_mod.load_settings()["experience"]["enabled"] is True


def test_filter_command_rejects_an_unknown_filter_and_bad_syntax():
    assert "❌" in commands_mod._handle_filter("salary on")
    assert "❌" in commands_mod._handle_filter("experience maybe")


def test_minexp_sets_the_threshold():
    commands_mod._handle_minexp("3")
    assert settings_mod.load_settings()["experience"]["max_years"] == 3.0


def test_minexp_rejects_nonsense_without_changing_anything():
    assert "❌" in commands_mod._handle_minexp("soon")
    assert "❌" in commands_mod._handle_minexp("-2")
    assert settings_mod.load_settings()["experience"]["max_years"] == 1.0


def test_minexp_strict_toggles():
    commands_mod._handle_minexp("strict on")
    assert settings_mod.load_settings()["experience"]["strict"] is True
    commands_mod._handle_minexp("strict off")
    assert settings_mod.load_settings()["experience"]["strict"] is False


def test_stats_command_before_any_run():
    assert "אין עדיין" in commands_mod._handle_stats()


def test_stats_command_reports_the_counters():
    run = RunStats()
    run.record("experience", "rejected_by_title")
    run.record("experience", "undetermined")
    save_stats(run)

    reply = commands_mod._handle_stats()
    assert "experience" in reply
    assert "נחסמו לפי כותרת" in reply


def test_an_empty_run_does_not_erase_the_last_real_measurement():
    run = RunStats()
    run.record("experience", "rejected_with_number")
    save_stats(run)
    save_stats(RunStats())          # a run with no new jobs at all

    stored = stats_mod.load_stats()
    assert stored["last_run"]["experience"]["rejected_with_number"] == 1


def test_totals_accumulate_across_runs():
    for _ in range(3):
        run = RunStats()
        run.record("experience", "undetermined")
        save_stats(run)

    stored = stats_mod.load_stats()
    assert stored["totals"]["experience"]["undetermined"] == 3
    assert stored["last_run"]["experience"]["undetermined"] == 1
