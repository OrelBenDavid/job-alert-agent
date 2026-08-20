# -*- coding: utf-8 -*-
"""The corpus-level check: which of the companies we watch can never deliver.

Every per-run check in this project compares a company against ITSELF. That is
blind to the failure that actually happened on 2026-08-19: `bird_aerosystems`,
`final`, `imagen` and `gk8` had never delivered a single alert in the project's
history, because the Comeet fetcher read a free-text label as if it were a
place. Their fetch succeeded, their count was a steady and plausible 0, and the
health gate had nothing to compare against. Only a report that looks across the
corpus sees "delivers nothing, ever".

The fixtures below are the real shapes that mattered: a board that returns
nothing, a board full of finance roles, a board whose postings all collapse
onto one id, and - just as importantly - the healthy companies that must NOT be
flagged, because a report that cries wolf is one nobody reads.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

import health_report
import state as state_mod


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fixture corpus - profiles on disk, because collect() runs the real loader
# --------------------------------------------------------------------------

def _profile(slug, platform="comeet", **health):
    """A thin company record over a real platform profile, so the fixture goes
    through the same _deep_merge and the same validation a live company does.
    A hand-rolled dict would let the test pass on a document the run would
    reject."""
    doc = {
        "slug": slug,
        "name": slug.replace("_", " ").title(),
        "platform": platform,
        "careers_url": "https://example.invalid/%s" % slug,
        "api": {"endpoint": "https://example.invalid/api/%s" % slug},
        "health": {"expected_min_jobs": health.get("floor", 0),
                   "zero_is_plausible": health.get("zero_ok", False)},
        "verified_on": health.get("verified_on", "2026-08-13"),
    }
    return doc


def _state(jobs, last_count=None, failures=0, seeded_at=None,
           last_success=None):
    seeded_at = seeded_at or (NOW - timedelta(days=30))
    stamp = seeded_at.isoformat()
    return {
        "last_success": (last_success or NOW - timedelta(hours=1)).isoformat(),
        "last_count": len(jobs) if last_count is None else last_count,
        "consecutive_failures": failures,
        "jobs": {job_id: {"title": title, "first_seen": stamp}
                 for job_id, title in jobs},
    }


@pytest.fixture
def corpus(tmp_path):
    """Writes a small corpus to disk and returns (profiles_dir, state_dir).

    The platform profiles are copied from the real ones rather than invented:
    the report reads `platform`, `detail_fetch` and `health` off the RESOLVED
    document, so a fixture that skipped the merge would be testing a different
    resolution path from the one that runs in production."""
    import profiles as profiles_mod

    profiles_dir = tmp_path / "profiles"
    (profiles_dir / "companies").mkdir(parents=True)
    (profiles_dir / "_platforms").mkdir()
    for platform in ("comeet", "greenhouse", "workday"):
        source = profiles_mod.PLATFORMS_DIR / ("%s.json" % platform)
        (profiles_dir / "_platforms" / ("%s.json" % platform)).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8")

    state_dir = tmp_path / "seen"
    state_dir.mkdir()

    def add(slug, jobs, platform="comeet", state=None, **kwargs):
        doc = _profile(slug, platform, **kwargs)
        (profiles_dir / "companies" / ("%s.json" % slug)).write_text(
            json.dumps(doc), encoding="utf-8")
        if state is not None or jobs is not None:
            payload = state if state is not None else _state(jobs)
            (state_dir / ("%s.json" % slug)).write_text(
                json.dumps(payload), encoding="utf-8")

    return profiles_dir, state_dir, add


def _collect(corpus):
    profiles_dir, state_dir, _ = corpus
    return health_report.collect(profiles_dir=profiles_dir,
                                 state_dir=state_dir, now=NOW)


def _by_slug(report):
    return {c.slug: c for c in report.companies}


# --------------------------------------------------------------------------
# The headline detector
# --------------------------------------------------------------------------

def test_an_empty_board_is_structurally_silent(corpus):
    """The exact shape of the four companies the Comeet bug hid behind: the
    fetch succeeds, the count is a plausible 0, and nothing else in the
    project can tell that apart from a company with no open roles."""
    _, _, add = corpus
    add("gk8", [])
    silent = _collect(corpus).silent
    assert [c.slug for c in silent] == ["gk8"]


def test_a_board_of_finance_roles_is_silent_too(corpus):
    """A board that fetches fine and tracks postings, none of which the role
    filter would ever pass. Not a scraper defect - a sourcing one, gate 4 in
    EXPANSION_STRATEGY.md section 7 - but the user sees the same nothing."""
    _, _, add = corpus
    add("bookaway", [("1", "Treasury Manager"), ("2", "Sales Director")])
    report = _collect(corpus)
    company = _by_slug(report)["bookaway"]
    assert company.structurally_silent and company.off_family_only
    assert company.blocked == 2 and company.deliverable == 0


def test_an_unclassifiable_title_counts_as_deliverable(corpus):
    """`unknown` is passed by the role filter and sent flagged - ~15% of
    postings - so a company whose board names no recognisable technology still
    delivers every one of them. `DFIR` is the real example roles.py cites for
    why the filter had to be a blocklist. Counting only `target` would report
    such a company as silent and send whoever reads this hunting a bug that is
    not there."""
    _, _, add = corpus
    add("cyops", [("1", "DFIR"), ("2", "Rotem")])
    report = _collect(corpus)
    company = _by_slug(report)["cyops"]
    assert company.unknown == 2 and company.deliverable == 2
    assert not company.structurally_silent


def test_an_evergreen_card_does_not_make_a_company_look_alive(corpus):
    """"Didn't find what you were looking for?" is a card, not a posting, and
    filters.RoleFilter drops it outright. A company whose only tracked entry is
    one of those delivers nothing, and must not be hidden from the report by
    its own placeholder."""
    _, _, add = corpus
    add("placeholder_co", [("1", "Didn't find what you were looking for?")])
    company = _by_slug(_collect(corpus))["placeholder_co"]
    assert company.non_job == 1 and company.deliverable == 0
    assert company.structurally_silent


def test_a_healthy_company_is_not_flagged(corpus):
    _, _, add = corpus
    add("mobileye", [("1", "Backend Engineer"), ("2", "VLSI Engineer")])
    report = _collect(corpus)
    assert report.silent == []
    assert report.deliverable_postings == 2


# --------------------------------------------------------------------------
# Duplicate ids - the defect this report found on its first live run
# --------------------------------------------------------------------------

def test_two_postings_sharing_one_id_are_reported(corpus):
    """Found live on 2026-08-20 at `neogames`: the Workday platform profile
    maps `api.fields.id` to `bulletFields.0`, documented as the requisition
    id. On the Aristocrat tenant that field holds the employment type, so every
    posting on the board arrives with the id "Regular", the state dict keeps
    one, and the diff - which runs on Job.id - can never see a new posting
    there again. last_count said 2, jobs held 1, and nothing else in the
    project compares those two numbers."""
    _, _, add = corpus
    add("neogames", None, platform="workday",
        state=_state([("Regular", "Bookkeeper")], last_count=2))
    report = _collect(corpus)
    assert [c.slug for c in report.id_collisions()] == ["neogames"]
    assert _by_slug(report)["neogames"].id_collisions == 1


def test_a_frozen_gate_is_not_mistaken_for_a_collision(corpus):
    """While the health gate holds, `last_count` describes the run it rejected
    and `jobs` describes an older, healthy one - so they are SUPPOSED to
    disagree. Reading that as duplicate ids would turn every gate hit into a
    second, invented alarm."""
    _, _, add = corpus
    add("pontera", None,
        state=_state([("1", "Security Engineer"), ("2", "Data Engineer")],
                     last_count=0, failures=3))
    report = _collect(corpus)
    assert report.id_collisions() == []


# --------------------------------------------------------------------------
# Health gate and staleness
# --------------------------------------------------------------------------

def test_a_company_parked_below_its_own_floor_is_reported(corpus):
    """The gate fires only on `count < floor <= previous`, and accepts the new
    count after three runs. A company that then SETTLES below its floor is
    invisible to it forever after - either the floor is stale (Datadog, lowered
    from 4 to 0 on 2026-08-19) or the fetch is truncated. Only a corpus-level
    read finds it."""
    _, _, add = corpus
    add("datadog", [("1", "Backend Engineer")], platform="greenhouse", floor=4)
    report = _collect(corpus)
    assert [c.slug for c in report.below_floor()] == ["datadog"]


def test_a_company_that_missed_every_run_for_a_day_is_stale(corpus):
    """The cron is every 3 hours. 24 hours without a success is eight
    consecutive misses while the rest of the corpus succeeded, which is past
    anything a transient outage explains - and it does NOT require
    consecutive_failures, because a company skipped by the fetch budget never
    bumps that counter at all."""
    _, _, add = corpus
    add("skipped", None,
        state=_state([("1", "Backend Engineer")],
                     last_success=NOW - timedelta(hours=30)))
    add("fresh", [("1", "Backend Engineer")])
    report = _collect(corpus)
    assert [c.slug for c in report.stale_success()] == ["skipped"]


def test_a_profile_verified_last_year_is_stale(corpus):
    _, _, add = corpus
    add("old", [("1", "Backend Engineer")], verified_on="2025-01-01")
    add("recent", [("1", "Backend Engineer")], verified_on="2026-08-13")
    report = _collect(corpus)
    assert [c.slug for c in report.stale_verification()] == ["old"]


# --------------------------------------------------------------------------
# The churn proxy, and its honesty about what it cannot say
# --------------------------------------------------------------------------

def test_a_company_too_young_to_judge_is_excluded_rather_than_cleared(corpus):
    """A freshly seeded company has by definition detected nothing, so counting
    it as "no churn" would bury the real cases under the whole of the last
    import batch. Measured on 2026-08-20: 356 of 368 companies were younger
    than the window."""
    _, _, add = corpus
    add("just_seeded", None,
        state=_state([("1", "Backend Engineer")],
                     seeded_at=NOW - timedelta(days=2)))
    report = _collect(corpus)
    assert report.churn_eligible() == []
    assert report.no_churn() == []


def test_a_posting_detected_after_seeding_clears_the_company(corpus):
    """Seeding stamps one timestamp onto every posting it writes, so anything
    with a later `first_seen` came through process_company's diff - a posting
    this project actually detected."""
    _, _, add = corpus
    seeded_at = NOW - timedelta(days=30)
    payload = _state([("1", "Backend Engineer")], seeded_at=seeded_at)
    payload["jobs"]["2"] = {"title": "Data Engineer",
                            "first_seen": (seeded_at
                                           + timedelta(days=1)).isoformat()}
    payload["last_count"] = 2
    add("moving", None, state=payload)
    add("frozen", None,
        state=_state([("1", "Backend Engineer"), ("2", "QA Engineer")],
                     seeded_at=seeded_at))
    report = _collect(corpus)
    assert _by_slug(report)["moving"].detected_since_seed == 1
    assert [c.slug for c in report.no_churn()] == ["frozen"]


# --------------------------------------------------------------------------
# Platform-level silence - the shape the Comeet bug actually made
# --------------------------------------------------------------------------

def test_one_platform_silent_together_is_flagged(corpus):
    """The Comeet bug was not one broken company, it was one field read wrongly
    on every board of one platform. Six silent Comeet boards against six
    healthy Greenhouse ones is that shape in miniature."""
    _, _, add = corpus
    for i in range(6):
        add("comeet_dead_%d" % i, [])
        add("gh_alive_%d" % i, [("1", "Backend Engineer")],
            platform="greenhouse")
    flagged = _collect(corpus).underperforming_platforms()
    assert [name for name, _, _ in flagged] == ["comeet"]


def test_a_platform_too_small_to_judge_is_not_flagged(corpus):
    """On a three-company platform one legitimately empty board is 33%, which
    is arithmetic, not evidence."""
    _, _, add = corpus
    add("wd_dead", [], platform="workday")
    add("wd_alive", [("1", "Backend Engineer")], platform="workday")
    for i in range(8):
        add("gh_alive_%d" % i, [("1", "Backend Engineer")],
            platform="greenhouse")
    assert _collect(corpus).underperforming_platforms() == []


# --------------------------------------------------------------------------
# Failure modes of the report itself
# --------------------------------------------------------------------------

def test_an_unreadable_state_file_is_surfaced_not_counted_as_healthy(corpus):
    """A company whose state cannot be parsed is skipped by run.py - it is
    silent in exactly the way this report exists to find, so it must never
    render as a plausible zero."""
    profiles_dir, state_dir, add = corpus
    add("broken", [("1", "Backend Engineer")])
    (state_dir / "broken.json").write_text("{not json", encoding="utf-8")
    report = _collect(corpus)
    company = _by_slug(report)["broken"]
    assert company.unreadable and "broken.json" in company.unreadable
    assert company.tracked == 0


def test_state_with_no_profile_is_reported_as_an_orphan(corpus):
    """A renamed or removed company leaves its history behind. `gong_io` was
    deleted on 2026-08-19 with its state file; the next such deletion should
    not need a human to notice."""
    profiles_dir, state_dir, add = corpus
    add("kept", [("1", "Backend Engineer")])
    (state_dir / "gong_io.json").write_text(
        json.dumps(_state([("1", "Backend Engineer")])), encoding="utf-8")
    assert _collect(corpus).orphan_state == ["gong_io"]


def test_a_company_with_no_state_file_is_a_seed_gap_not_a_silent_company(corpus):
    """Never seeded is a different problem with a different fix (`run.py
    --seed`), and folding it into the silent count would send someone auditing
    a fetcher that has never been run."""
    _, _, add = corpus
    add("unseeded", None)
    report = _collect(corpus)
    assert [c.slug for c in report.unseeded] == ["unseeded"]
    assert report.silent == []


def test_an_unparseable_timestamp_never_manufactures_a_silent_company(corpus):
    """Hand-edited state happens. The report must degrade to "unknown" on a bad
    value, never to a finding - a false positive here costs someone an audit of
    a company that is fine."""
    _, _, add = corpus
    payload = _state([("1", "Backend Engineer")])
    payload["jobs"]["1"]["first_seen"] = "not-a-timestamp"
    payload["last_success"] = "also-not"
    add("wonky", None, state=payload)
    report = _collect(corpus)
    company = _by_slug(report)["wonky"]
    assert company.last_success is None
    assert company.detected_since_seed == 1   # errs toward "this one is alive"
    assert not company.structurally_silent


# --------------------------------------------------------------------------
# The read-only guarantee
# --------------------------------------------------------------------------

def test_the_report_writes_nothing(corpus, monkeypatch):
    """A diagnostic that can modify what it diagnoses makes every finding
    unanswerable ("did the report cause it?"). This is enforced by import
    discipline, so the test asserts on the actual filesystem rather than
    trusting the reading."""
    profiles_dir, state_dir, add = corpus
    add("alive", [("1", "Backend Engineer")])
    add("dead", [])

    def explode(*args, **kwargs):
        raise AssertionError("health_report must never write state")

    monkeypatch.setattr(state_mod, "_write_state", explode)
    monkeypatch.setattr(state_mod, "_write_run_state", explode)

    before = {p: p.read_bytes() for p in
              list(state_dir.rglob("*")) + list(profiles_dir.rglob("*"))
              if p.is_file()}
    report = _collect(corpus)
    health_report.format_text(report)
    after = {p: p.read_bytes() for p in
             list(state_dir.rglob("*")) + list(profiles_dir.rglob("*"))
             if p.is_file()}
    assert before == after


def test_load_state_default_directory_is_unchanged(tmp_path):
    """The state_dir parameter added for this report must not have moved the
    default. Every writer still resolves through STATE_DIR, and a reader that
    quietly started reading somewhere else would be worse than no report."""
    assert state_mod._state_path("x") == state_mod.STATE_DIR / "x.json"
    assert state_mod._state_path("x", tmp_path) == tmp_path / "x.json"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_the_text_report_names_the_silent_companies(corpus):
    """The report's whole value is that someone reads it, so the slug has to be
    in the output and not only in the count."""
    _, _, add = corpus
    add("gk8", [])
    add("bookaway", [("1", "Treasury Manager")])
    text = health_report.format_text(_collect(corpus))
    assert "gk8" in text and "bookaway" in text
    assert "STRUCTURALLY SILENT" in text


def test_the_telegram_summary_escapes_everything_telegram_rejects(corpus):
    """A MarkdownV2 message with one unescaped `.` or `_` is a 400 on the whole
    reply, so /health would fail silently and permanently. Company slugs carry
    underscores as a rule here."""
    from notifier import _MDV2_SPECIAL

    _, _, add = corpus
    add("media_force", [("1", "Sales Director")])
    add("ai21_labs", [])
    text = health_report.format_telegram(_collect(corpus))

    assert "media\\_force" in text and "ai21\\_labs" in text
    # `*` is the only unescaped special the message is allowed to contain: it
    # is the heading emphasis, and it is deliberately the ONLY syntax used, so
    # that this check can be absolute about everything else.
    for index, char in enumerate(text):
        if char in _MDV2_SPECIAL and char != "*":
            assert text[index - 1] == "\\", (
                "unescaped %r at %d in: %s" % (char, index, text))
    assert len(text) < 4096
