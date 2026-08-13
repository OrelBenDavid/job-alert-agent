# -*- coding: utf-8 -*-
"""
The embedded_json detail method - description text held in a JSON blob inside
a page's <script>, rather than in the DOM or the listing response.

Written for Comeet, which is 105 of the project's 145 companies. Until it
existed the Comeet platform profile declared `detail_fetch: none`, so the
experience filter could not evaluate 72% of the corpus and every one of those
jobs was delivered tagged "undetermined". That is why the coverage here is
heavier than the size of the function suggests: a silent regression in this
path does not raise, it just quietly stops determining anything, which looks
exactly like a company that writes no requirements.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import detail as detail_mod
from detail import DetailBudget, _extract_from_embedded_json, enrich
from experience import read_experience
from models import Job

CFG = {
    "method": "embedded_json",
    "embedded_json_var": "POSITION_DATA",
    "embedded_json_path": "custom_fields.details",
    "inline_section_heading": "name",
    "inline_section_content": "value",
    "content_is_html": True,
}


def _page(position: dict, company: dict | None = None) -> str:
    """A page shaped like Comeet's: the company object is emitted FIRST and
    the variable name is mentioned before its own assignment."""
    company = company if company is not None else {"name": "Acme",
                                                   "custom_fields": {}}
    return (
        "<html><head><script>var serverVersion='x';</script>"
        # The trap: POSITION_DATA named before it is assigned, with another
        # object in between. Anchoring on the name alone parses this one.
        "<script>if (window.POSITION_DATA) { init(); } "
        f"var COMPANY_DATA = {json.dumps(company)};</script>"
        f"<script>var POSITION_DATA = {json.dumps(position)};</script>"
        "</head><body><div ng-if='hasDescription()'></div></body></html>")


REQUIREMENTS = ("<p><strong>Mandatory Requirements</strong></p><ul>"
                "<li>7+ years experience in Account Management.</li></ul>")


def _position(details):
    return {"name": "Account Manager", "uid": "2D.A6D",
            "custom_fields": {"details": details}}


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_it_extracts_the_sections_and_keeps_the_heading():
    """The section NAME must survive as a real heading. That heading is what
    promotes the unmarked bullets under it to 'mandatory' in
    experience.classify_block - without it the text parses to nothing."""
    markup = _page(_position([
        {"name": "Description", "value": "<p>Great team.</p>", "order": 0},
        {"name": "Requirements", "value": REQUIREMENTS, "order": 1},
    ]))
    out = _extract_from_embedded_json(markup, CFG)
    assert "<h3>Requirements</h3>" in out
    assert "7+ years" in out


def test_the_extracted_text_actually_parses_to_a_number():
    """End to end through the real parser - the property that matters."""
    markup = _page(_position([
        {"name": "Requirements", "value": REQUIREMENTS, "order": 1}]))
    out = _extract_from_embedded_json(markup, CFG)
    assert read_experience(out).min_years == 7.0


def test_it_anchors_on_the_assignment_not_the_first_mention():
    """The bug this was actually written against, and the nastiest kind: the
    variable name appears before its assignment, and anchoring on the name
    parsed the COMPANY object instead. custom_fields there is empty, so every
    posting came back with zero sections and looked exactly like a company
    that writes no requirements - no error, no log line, just silence."""
    markup = _page(
        _position([{"name": "Requirements", "value": REQUIREMENTS, "order": 1}]),
        company={"name": "Acme Inc", "custom_fields": {}})
    out = _extract_from_embedded_json(markup, CFG)
    assert out is not None, "parsed the wrong object - see the docstring"
    assert "7+ years" in out


def test_unicode_escaped_html_survives():
    """Comeet emits the value with \\u003C escapes; json.loads undoes them."""
    escaped = json.loads('"\\u003Cul\\u003E\\u003Cli\\u003E5+ years required'
                         '\\u003C/li\\u003E\\u003C/ul\\u003E"')
    markup = _page(_position([{"name": "Requirements", "value": escaped}]))
    out = _extract_from_embedded_json(markup, CFG)
    assert "<li>" in out and "5+ years" in out


@pytest.mark.parametrize("markup,why", [
    ("<html><body>nothing here</body></html>", "no variable at all"),
    ("<script>var POSITION_DATA = {not json;</script>", "malformed JSON"),
])
def test_a_page_it_cannot_read_returns_none_rather_than_raising(markup, why):
    """Fail-open is the contract of this whole layer: an unreadable page means
    'undetermined', which passes and is flagged - never a company failure."""
    assert _extract_from_embedded_json(markup, CFG) is None, why


def test_a_path_that_does_not_exist_returns_none():
    markup = _page({"name": "X", "custom_fields": {}})
    assert _extract_from_embedded_json(markup, CFG) is None


def test_a_string_at_the_path_is_returned_as_is():
    """The path need not point at a section list - a plain string works too."""
    cfg = dict(CFG, embedded_json_path="description")
    markup = _page({"description": "<p>3+ years required</p>"})
    assert _extract_from_embedded_json(markup, cfg) == "<p>3+ years required</p>"


# ---------------------------------------------------------------------------
# Integration with enrich()
# ---------------------------------------------------------------------------

class _Profile:
    slug = "acme"

    def __init__(self, cfg):
        self.detail_fetch = cfg


def _jobs(n=2):
    return [Job(id=f"j{i}", title=f"Job {i}", location="Tel Aviv",
                url=f"https://www.comeet.com/jobs/acme/AA.001/x/{i}",
                company="acme") for i in range(n)]


def _response(markup):
    r = MagicMock()
    r.text = markup
    r.raise_for_status.return_value = None
    return r


def test_enrich_fetches_one_page_per_posting_and_fills_descriptions():
    markup = _page(_position([{"name": "Requirements", "value": REQUIREMENTS}]))
    session = MagicMock()
    session.get.return_value = _response(markup)

    with patch.object(detail_mod.requests, "Session", return_value=session):
        out = enrich(_jobs(2), _Profile(CFG), DetailBudget(remaining=10))

    assert session.get.call_count == 2
    assert all(read_experience(j.description).min_years == 7.0 for j in out)


def test_one_bad_page_costs_only_its_own_job():
    """Fail-open, per posting. A company-wide failure here would be a
    regression against this layer's single hard rule."""
    good = _page(_position([{"name": "Requirements", "value": REQUIREMENTS}]))
    session = MagicMock()
    session.get.side_effect = [RuntimeError("502"), _response(good)]

    with patch.object(detail_mod.requests, "Session", return_value=session):
        out = enrich(_jobs(2), _Profile(CFG), DetailBudget(remaining=10))

    assert len(out) == 2                     # nothing dropped
    assert out[0].description is None        # the failure, undetermined
    assert read_experience(out[1].description).min_years == 7.0


def test_it_respects_the_run_wide_budget():
    """The budget is shared across all companies - at 105 Comeet companies an
    unbounded path would be thousands of requests and would take the workflow
    timeout, and a timed-out run commits no state at all."""
    markup = _page(_position([{"name": "Requirements", "value": REQUIREMENTS}]))
    session = MagicMock()
    session.get.return_value = _response(markup)

    with patch.object(detail_mod.requests, "Session", return_value=session):
        out = enrich(_jobs(5), _Profile(CFG), DetailBudget(remaining=2))

    assert session.get.call_count == 2
    assert len(out) == 5                          # the rest are still returned
    assert out[3].description is None             # just undetermined


def test_wants_detail_fetch_counts_embedded_json_as_costing_requests():
    """It issues one GET per posting, so a disabled filter must not trigger
    it - that is what wants_detail_fetch gates."""
    from detail import wants_detail_fetch
    assert wants_detail_fetch(_Profile(CFG)) is True
    assert wants_detail_fetch(_Profile({"method": "inline"})) is False
    assert wants_detail_fetch(_Profile(None)) is False
