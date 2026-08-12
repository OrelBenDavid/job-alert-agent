# -*- coding: utf-8 -*-
"""
Tests for the detail layer - overwhelmingly about what happens when it
FAILS, since that is the behaviour the whole design depends on: a detail
failure resolves to "undetermined" (which sends the job, flagged) and is
never allowed to become a company failure.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import detail as detail_mod
from detail import (enrich, normalize_description, normalize_inline_value,
                    render_sections, wants_detail_fetch)
from experience import read_experience
from models import Job


def _job(job_id="1"):
    return Job(id=job_id, title="Backend Developer", location="Tel Aviv",
               url=f"https://example.com/{job_id}", company="acme")


def _profile(detail_fetch):
    return SimpleNamespace(slug="acme", detail_fetch=detail_fetch)


_HTML_CFG = {"method": "html", "url_source": "job_url",
             "content_selector": "#job-description", "content_is_html": True}


def _response(text, status_ok=True):
    r = MagicMock()
    r.text = text
    r.raise_for_status.side_effect = None if status_ok else Exception("404")
    return r


# ---------------------------------------------------------------------------
# The escaped-HTML trap
# ---------------------------------------------------------------------------

def test_escaped_html_is_unescaped():
    """Greenhouse's content is both HTML and HTML-escaped. Skipping the
    unescape yields text literally full of "&lt;p&gt;"."""
    out = normalize_description("&lt;p&gt;3+ years&lt;/p&gt;", content_is_html=True)
    assert out == "<p>3+ years</p>"


def test_plain_text_content_is_left_alone():
    assert normalize_description("3+ years", content_is_html=False) == "3+ years"


def test_blank_content_reads_as_no_description():
    assert normalize_description("   ", content_is_html=True) is None
    assert normalize_description(None, content_is_html=True) is None


# ---------------------------------------------------------------------------
# Structured inline fields - the shape Lever actually returns
# ---------------------------------------------------------------------------

# Exactly the shape verified live on Mobileye: a list of sections, each with a
# recruiter-written heading and a bare run of <li> with no <ul> around it.
_LEVER_LISTS = [
    {"text": "What will your job look like:",
     "content": "<li>Develop algorithms</li><li>Work with the vision team</li>"},
    {"text": "All you need is:",
     "content": "<li>B.Sc. in Computer Science</li>"
                "<li>3+ years of experience with Python</li>"},
    {"text": "Nice to have:",
     "content": "<li>8+ years of experience with C++</li>"},
]


def test_sections_render_headings_as_real_headings():
    """The heading has to survive as a heading - it is what promotes an
    unmarked bullet to mandatory."""
    out = render_sections(_LEVER_LISTS)
    assert "<h3>All you need is:</h3>" in out
    assert "<ul><li>B.Sc. in Computer Science</li>" in out


def test_a_structured_field_end_to_end_yields_the_right_number():
    """The whole point: 3+ under "All you need is:" counts, while 8+ under
    "Nice to have:" must not."""
    description = normalize_inline_value(_LEVER_LISTS, content_is_html=True)
    assert read_experience(description).min_years == 3.0


def test_the_intro_only_fields_really_do_carry_nothing():
    """Verified live: Lever's `description` holds the intro paragraph only,
    which is why inline_field must point at `lists`."""
    intro = "<p>Mobileye is looking for a 3D Algorithm Developer.</p>"
    assert read_experience(normalize_inline_value(intro, True)).min_years is None


def test_section_field_names_are_overridable():
    sections = [{"heading": "Requirements:", "html": "<li>2+ years of experience</li>"}]
    out = normalize_inline_value(sections, True,
                                 heading_field="heading", content_field="html")
    assert read_experience(out).min_years == 2.0


def test_a_string_inline_field_still_works():
    assert normalize_inline_value("&lt;p&gt;hi&lt;/p&gt;", True) == "<p>hi</p>"


@pytest.mark.parametrize("raw", [None, 42, {"not": "a list"}, [], ["bare string"]])
def test_unusable_inline_values_fail_open(raw):
    """An inline field of an unexpected shape reads as no description, which
    passes - never as a crash mid-run."""
    assert normalize_inline_value(raw, True) is None


# ---------------------------------------------------------------------------
# Which profiles cost requests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cfg,expected", [
    (None, False),                                        # v2 profile
    ({"method": "inline", "inline_field": "content"}, False),   # zero requests
    (_HTML_CFG, True),
    ({"method": "playwright", "content_selector": "#d"}, True),
])
def test_wants_detail_fetch(cfg, expected):
    assert wants_detail_fetch(_profile(cfg)) is expected


def test_a_v2_profile_returns_jobs_untouched():
    jobs = [_job("1"), _job("2")]
    assert enrich(jobs, _profile(None)) is jobs


def test_inline_costs_no_requests():
    """The listing already carried the description; enrich must not fetch."""
    jobs = [_job("1")]
    with patch.object(detail_mod.requests, "Session") as session:
        out = enrich(jobs, _profile({"method": "inline",
                                     "inline_field": "content"}))
    session.assert_not_called()
    assert out is jobs


# ---------------------------------------------------------------------------
# The html path, and its failure modes
# ---------------------------------------------------------------------------

def test_html_path_extracts_the_content_selector():
    page = ("<html><body><div id='job-description'>"
            "<ul><li>3+ years of experience</li></ul>"
            "</div></body></html>")
    with patch.object(detail_mod.requests, "Session") as session_cls:
        session_cls.return_value.get.return_value = _response(page)
        out = enrich([_job("1")], _profile(_HTML_CFG))
    assert "3+ years of experience" in out[0].description


def test_a_dead_selector_yields_no_description_not_an_error():
    with patch.object(detail_mod.requests, "Session") as session_cls:
        session_cls.return_value.get.return_value = _response(
            "<html><body><p>redesigned page</p></body></html>")
        out = enrich([_job("1")], _profile(_HTML_CFG))
    assert out[0].description is None      # -> undetermined -> sent, flagged


def test_a_404_does_not_raise_and_does_not_lose_the_job():
    with patch.object(detail_mod.requests, "Session") as session_cls:
        session_cls.return_value.get.return_value = _response("", status_ok=False)
        out = enrich([_job("1"), _job("2")], _profile(_HTML_CFG))
    assert len(out) == 2
    assert all(job.description is None for job in out)


def test_one_failure_does_not_affect_the_other_jobs():
    good = ("<div id='job-description'><ul>"
            "<li>2+ years of experience</li></ul></div>")

    def get(url, **kwargs):
        if url.endswith("/2"):
            raise Exception("timeout")
        return _response(good)

    with patch.object(detail_mod.requests, "Session") as session_cls:
        session_cls.return_value.get.side_effect = get
        out = enrich([_job("1"), _job("2"), _job("3")], _profile(_HTML_CFG))

    assert [job.description is not None for job in out] == [True, False, True]


def test_the_returned_list_always_matches_the_input_length_and_order():
    """A caller must never be able to lose a job to the detail layer."""
    with patch.object(detail_mod.requests, "Session") as session_cls:
        session_cls.return_value.get.side_effect = Exception("everything is down")
        jobs = [_job(str(i)) for i in range(5)]
        out = enrich(jobs, _profile(_HTML_CFG))
    assert [job.id for job in out] == [job.id for job in jobs]


def test_the_per_run_cap_stops_fetching_but_keeps_every_job(monkeypatch):
    """The overflow fails open like every other miss - the jobs past the cap
    are still returned, just undetermined."""
    monkeypatch.setattr(detail_mod, "MAX_DETAIL_FETCHES_PER_RUN", 3)
    page = "<div id='job-description'><ul><li>1 year of experience</li></ul></div>"

    with patch.object(detail_mod.requests, "Session") as session_cls:
        session_cls.return_value.get.return_value = _response(page)
        jobs = [_job(str(i)) for i in range(10)]
        out = enrich(jobs, _profile(_HTML_CFG))
        assert session_cls.return_value.get.call_count == 3

    assert len(out) == 10
    assert [job.description is not None for job in out[:3]] == [True] * 3
    assert all(job.description is None for job in out[3:])


# ---------------------------------------------------------------------------
# URL sources
# ---------------------------------------------------------------------------

def test_url_template_substitutes_the_job_id():
    cfg = dict(_HTML_CFG, url_source="template",
               url_template="https://api.example.com/jobs/{id}/detail")
    with patch.object(detail_mod.requests, "Session") as session_cls:
        session_cls.return_value.get.return_value = _response("")
        enrich([_job("abc123")], _profile(cfg))
        called_url = session_cls.return_value.get.call_args[0][0]
    assert called_url == "https://api.example.com/jobs/abc123/detail"


def test_requirements_section_selector_falls_back_when_it_misses():
    """It is a hint, not a mechanism - a miss must degrade to the full
    content, not to nothing. Real postings title their requirements block
    whatever the recruiter felt like."""
    cfg = dict(_HTML_CFG, requirements_section_selector="#requirements")
    page = ("<div id='job-description'><ul>"
            "<li>3+ years of experience</li></ul></div>")
    with patch.object(detail_mod.requests, "Session") as session_cls:
        session_cls.return_value.get.return_value = _response(page)
        out = enrich([_job("1")], _profile(cfg))
    assert "3+ years of experience" in out[0].description
