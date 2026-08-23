# -*- coding: utf-8 -*-
"""
Staged seeding: --limit and --only.

Seeding is the one irreversible step in adding a company. It decides which
postings count as "already known" and are therefore never alerted - so a bug
here does not raise, it silently swallows real jobs. The tests below are mostly
about what must NOT happen: no double-seeding, no silent skip of a mistyped
slug, and above all no alert of any kind.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import run as run_mod
import state as state_mod
from fetchers import FetchOutcome
from models import Job


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state_mod, "STATE_DIR", tmp_path / "seen")
    monkeypatch.setattr(state_mod, "RUN_STATE_PATH", tmp_path / "run.json")
    (tmp_path / "seen").mkdir()
    return tmp_path


class _FakeProfile:
    fetch_type = "api"

    def __init__(self, slug):
        self.slug = slug
        self.name = slug.title()


def _fake_profiles(n):
    return [_FakeProfile(f"co{i:02d}") for i in range(n)]


def _fake_fetch_all(profiles, deadline=None):
    return {p.slug: FetchOutcome(
        p.slug,
        [Job(id=f"{p.slug}-{i}", title=f"Job {i}", location="Tel Aviv",
             url=f"https://x/{p.slug}/{i}", company=p.slug) for i in range(3)],
        None, 0.1) for p in profiles}


@pytest.fixture
def seeding(isolated_state, monkeypatch):
    """A seed run wired to fake profiles and fetches, with every notifier
    entry point patched to explode - a seed that sends anything is a bug, and
    the test should fail loudly rather than assert on a mock afterwards."""
    profiles = _fake_profiles(10)
    monkeypatch.setattr(run_mod, "load_enabled", lambda: (profiles, []))
    monkeypatch.setattr(run_mod, "fetch_all", _fake_fetch_all)

    def explode(*a, **k):
        raise AssertionError("a seed run must never send anything")

    monkeypatch.setattr(run_mod, "notify_new_jobs", explode)
    monkeypatch.setattr(run_mod, "notify_maintenance", explode)
    monkeypatch.setattr(run_mod, "notify_maintenance_digest", explode)
    return profiles


def _seeded(profiles):
    return [p.slug for p in profiles
            if state_mod.load_state(p.slug).get("last_success") is not None]


# ---------------------------------------------------------------------------
# --limit
# ---------------------------------------------------------------------------

def test_limit_seeds_only_that_many(seeding):
    run_mod._run_seed(limit=4)
    assert len(_seeded(seeding)) == 4


def test_repeated_limited_runs_walk_the_backlog_without_repeating(seeding):
    """The whole point of batching: each run picks up exactly the companies
    the previous ones did not, because the command skips what is seeded."""
    run_mod._run_seed(limit=4)
    first = set(_seeded(seeding))
    run_mod._run_seed(limit=4)
    second = set(_seeded(seeding)) - first

    assert len(first) == 4 and len(second) == 4
    assert not (first & second)          # no company seeded twice

    run_mod._run_seed(limit=4)
    assert len(_seeded(seeding)) == 10    # backlog exhausted, not overshot


def test_batches_are_stable_across_runs(seeding):
    """Profiles load sorted by path, so batch 1 is the same set every time.
    An arbitrary order would make a staged seed unreviewable."""
    run_mod._run_seed(limit=3)
    assert sorted(_seeded(seeding)) == ["co00", "co01", "co02"]


def test_a_limit_larger_than_the_backlog_is_fine(seeding):
    run_mod._run_seed(limit=999)
    assert len(_seeded(seeding)) == 10


def test_no_limit_seeds_everything(seeding):
    run_mod._run_seed()
    assert len(_seeded(seeding)) == 10


def test_seeding_records_ids_and_sends_nothing(seeding):
    """If any notifier were reached the fixture would raise."""
    run_mod._run_seed(limit=1)
    state = state_mod.load_state("co00")
    assert set(state["jobs"]) == {"co00-0", "co00-1", "co00-2"}
    assert state["last_count"] == 3


# ---------------------------------------------------------------------------
# --only
# ---------------------------------------------------------------------------

def test_only_seeds_the_named_companies(seeding, tmp_path):
    listing = tmp_path / "batch.txt"
    listing.write_text("co07\nco03\n", encoding="utf-8")
    run_mod._run_seed(only=str(listing))
    assert sorted(_seeded(seeding)) == ["co03", "co07"]


def test_only_ignores_comments_and_blank_lines(seeding, tmp_path):
    listing = tmp_path / "batch.txt"
    listing.write_text("# tier A first\n\nco01  # the big one\n\n",
                       encoding="utf-8")
    run_mod._run_seed(only=str(listing))
    assert _seeded(seeding) == ["co01"]


def test_an_unknown_slug_in_the_list_is_reported_not_silently_skipped(
        seeding, tmp_path, capsys):
    """A typo'd slug that passes quietly leaves the company looking seeded
    when nothing happened to it - and it would then never be alerted about
    until someone noticed the seed gap."""
    listing = tmp_path / "batch.txt"
    listing.write_text("co01\ntypo-co99\n", encoding="utf-8")
    run_mod._run_seed(only=str(listing))
    assert "typo-co99" in capsys.readouterr().err
    assert _seeded(seeding) == ["co01"]


def test_only_and_limit_compose(seeding, tmp_path):
    listing = tmp_path / "batch.txt"
    listing.write_text("co01\nco02\nco03\n", encoding="utf-8")
    run_mod._run_seed(only=str(listing), limit=2)
    assert len(_seeded(seeding)) == 2


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def test_a_bare_run_is_not_a_seed():
    assert run_mod._parse_args([]).seed is False


def test_seed_flags_parse():
    args = run_mod._parse_args(["--seed", "--limit", "25"])
    assert args.seed is True and args.limit == 25


@pytest.mark.parametrize("argv", [
    ["--limit", "5"],        # batching a normal run has no coherent meaning
    ["--only", "x.txt"],
    ["--force"],
])
def test_seed_only_flags_are_rejected_without_seed(argv):
    """Offering these on a normal run would invite a partial scheduled run
    that looks complete."""
    with pytest.raises(SystemExit):
        run_mod._parse_args(argv)


def test_a_zero_or_negative_limit_is_rejected():
    for bad in ("0", "-1"):
        with pytest.raises(SystemExit):
            run_mod._parse_args(["--seed", "--limit", bad])
