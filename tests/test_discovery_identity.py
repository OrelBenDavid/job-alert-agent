# -*- coding: utf-8 -*-
"""The identity gate in _onboarding/discover_ats.py.

*** Why this file exists ***

Discovery guesses a slug and asks an ATS "does this board exist". A board that
says yes is not evidence that it belongs to the company whose name produced the
guess - and when it doesn't, the failure is the silent kind this project cares
most about: the profile loads, the fetch returns 200, the health gate sees a
healthy count, and the bot monitors a different company forever.

Every collision below is REAL, taken from the first live run of `discover` over
100 unprofiled companies on 2026-08-18, where roughly a third of the raw hits
were other people's companies. These are regression tests for the gate that now
rejects them, and they are offline - no probe here touches the network, matching
the rest of the suite.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_onboarding"))

import discover_ats as D


# ---------------------------------------------------------------------------
# slugs_for - the bare first word is gone


def test_first_word_is_not_a_candidate_for_multiword_names():
    """The single change that removed six wrong-company matches.

    "Align", "Novo", "Change" and "Allied" are other companies' entire names,
    so a first word is not evidence of identity.
    """
    for name in ("Align Technology", "Novo Nordisk", "Change Healthcare",
                 "Cornerstone OnDemand", "Samsung Semiconductors"):
        first = name.split()[0].lower()
        assert first not in D.slugs_for(name), (
            f"{name!r} still proposes the bare first word {first!r}")


def test_single_word_names_still_propose_themselves():
    """The rule targets fragments, not short names - Wolt must stay findable."""
    assert "wolt" in D.slugs_for("Wolt")
    assert "tenable" in D.slugs_for("Tenable")


def test_corporate_suffixes_are_still_generated():
    """They earned two genuine hits (tenableinc, innodatainc) and their one
    false positive is now caught by names_match instead of by removal."""
    assert "tenableinc" in D.slugs_for("Tenable")
    assert "innodatainc" in D.slugs_for("Innodata")


# ---------------------------------------------------------------------------
# names_match


@pytest.mark.parametrize("seed, board", [
    ("Tenable", "Tenable, Inc."),          # live: greenhouse `tenableinc`
    ("Innodata", "Innodata Inc"),
    ("Wolt", "Wolt"),
    ("Viz.ai", "Viz.ai"),                  # live: ashby, literal dot
    ("Cornerstone OnDemand", "Cornerstone"),
    ("Palantir", "Palantir Technologies"),  # live: lever board page title
])
def test_same_company_matches(seed, board):
    assert D.names_match(seed, board)


@pytest.mark.parametrize("seed, board", [
    ("Align Technology", "A-LIGN External"),   # live greenhouse collision
    ("AT&T", "Attio"),                          # live ashby collision via +io
    ("Change Healthcare", "Change.org"),
    ("Novo Nordisk", "Novolabs"),
])
def test_different_company_is_rejected(seed, board):
    assert not D.names_match(seed, board)


def test_prefix_alone_is_not_enough():
    """The specific rule that saves AT&T from Attio: a prefix match is only
    accepted when the leftover is a corporate suffix, and "io" is not one."""
    assert not D.names_match("AT&T", "Attio")
    assert D.names_match("Tenable", "Tenable Ltd")


def test_empty_or_unknown_name_never_matches():
    """A board that publishes no usable name is unverifiable, not verified."""
    assert not D.names_match("Lemonade", "")
    assert not D.names_match("", "Lemonade")


# ---------------------------------------------------------------------------
# looks_like_demo - name matching alone cannot catch these


def test_vendor_demo_board_is_detected():
    """Recruitee's `google`, `meta` and `samsung` boards are each a single
    posting called "Senior Marketer (Sample)" - and each one passes a name
    check perfectly, because the board really is named "Google"."""
    assert D.looks_like_demo([("Senior Marketer (Sample)", "London")])


def test_real_board_is_not_a_demo():
    assert not D.looks_like_demo([
        ("Backend Engineer", "Tel Aviv"),
        ("QA Engineer", "Tel Aviv"),
        ("DevOps Engineer", "Herzliya"),
    ])


def test_a_real_one_posting_board_is_not_a_demo():
    """A company with a single genuine opening must survive - the signal is the
    sample-ish TITLE, not the count."""
    assert not D.looks_like_demo([("Senior Backend Engineer", "Tel Aviv")])


def test_empty_board_is_not_a_demo():
    """Emptiness is handled separately, by requiring a verified name."""
    assert not D.looks_like_demo([])


# ---------------------------------------------------------------------------
# confidence_for - the three answers, with the network stubbed


def _stub_board_name(monkeypatch, value):
    monkeypatch.setattr(D, "board_name", lambda platform, slug: value)


def test_matching_board_name_is_verified(monkeypatch):
    _stub_board_name(monkeypatch, "Tenable, Inc.")
    assert D.confidence_for("Tenable", "greenhouse", "tenableinc",
                            [("Backend Engineer", "Tel Aviv")]) == "verified"


def test_mismatched_board_name_is_rejected(monkeypatch):
    _stub_board_name(monkeypatch, "A-LIGN External")
    assert D.confidence_for("Align Technology", "greenhouse", "align",
                            [("Auditor", "Atlanta")]) == "rejected"


def test_platform_without_a_name_is_unverifiable(monkeypatch):
    """Lever, Ashby and SmartRecruiters can all land here. 'unverifiable' must
    stay distinct from 'verified' so the import step can ask for a human
    glance rather than silently trusting a guess."""
    _stub_board_name(monkeypatch, None)
    assert D.confidence_for("Some Company", "smartrecruiters", "somecompany",
                            [("Engineer", "Tel Aviv")]) == "unverifiable"


def test_demo_board_is_rejected_before_the_name_is_consulted(monkeypatch):
    """The demo check has to run first: Recruitee's demo board is genuinely
    named "Google", so a name check alone would call it verified."""
    _stub_board_name(monkeypatch, "Google")
    assert D.confidence_for("Google", "recruitee", "google",
                            [("Senior Marketer (Sample)", "London")]) == "rejected"


# ---------------------------------------------------------------------------
# Platform routing


def test_comeet_and_workday_are_not_slug_guessable():
    """Both are reachable only from a careers page, for different reasons: a
    Comeet probe costs a ~750 KB board page per candidate, and a Workday
    tenant/site pair cannot be derived from a company name at all. Guessing
    either would be expensive and wrong respectively."""
    assert "comeet" not in D.GUESSABLE
    assert "workday" not in D.GUESSABLE
    # ...but both must still be probeable once an id is known.
    assert "comeet" in D.PROBES
    assert "workday" in D.PROBES


def test_every_guessable_platform_has_a_probe():
    for platform in D.GUESSABLE:
        assert platform in D.PROBES, f"{platform} is guessable but has no probe"
