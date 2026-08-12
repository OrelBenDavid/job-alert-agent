"""The first-page/later-page distinction in the browser fetcher.

Regression cover for a live failure: careers.wix.com renders its job cards
4-9s after `load` fires, and _fetch_url_pages used to treat a first-page
selector timeout the same way it treats a page-7 timeout - as the end of
pagination. The company came back with ZERO jobs and no exception, which is
indistinguishable from "no open roles" and surfaces downstream as a false
broken-selector alert.

It also gets much worse under the concurrent fetch: measured on the real
site, three Chromiums at once turned a healthy 16-job fetch into 0 jobs on
all three.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fetchers import browser as browser_mod
from fetchers.browser import ListingNeverRendered


class _FakeProfile:
    slug = "fake"
    careers_url = "https://careers.example.com/positions"
    israel_filter = {"method": "post_fetch", "structure": "none"}


CFG = {
    "job_selector": ".card",
    "title_selector": ".title",
    "location_selector": ".loc",
    "link_selector": "a",
    "link_base": "https://careers.example.com",
    "pagination": {"method": "url_param", "param_name": "page",
                   "start_value": 1, "max_pages": 5},
}


def _card(title="Backend Engineer", location="Tel Aviv", href="/jobs/1"):
    """A DOM job card whose three sub-selectors resolve like the real one."""
    card = MagicMock()
    def query(selector):
        el = MagicMock()
        if selector == ".title":
            el.inner_text.return_value = title
        elif selector == ".loc":
            el.inner_text.return_value = location
        elif selector == "a":
            el.get_attribute.return_value = href
            return el
        return el
    card.query_selector.side_effect = query
    return card


def test_first_page_timeout_raises_instead_of_returning_nothing():
    """The core regression. Two attempts both time out -> an exception, so
    run.py records a failure and leaves state alone, rather than a silent []
    that reads as 'this company has no jobs'."""
    page = MagicMock()
    page.wait_for_selector.side_effect = TimeoutError("selector never appeared")

    with pytest.raises(ListingNeverRendered) as excinfo:
        browser_mod._fetch_url_pages(page, _FakeProfile(), CFG)

    assert "NOT as zero open jobs" in str(excinfo.value)
    # it retried once before giving up, rather than failing on first sight
    assert page.wait_for_selector.call_count == 2


def test_first_page_is_retried_once_and_a_transient_timeout_recovers():
    """The measured real-world behaviour: a slow first load that succeeds
    immediately on a second attempt must produce a NORMAL run, not an alert."""
    page = MagicMock()
    attempts = {"n": 0}

    def wait(selector, timeout=None, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise TimeoutError("cold cache")
        return MagicMock()

    page.wait_for_selector.side_effect = wait
    # page 1 has cards; page 2 is the real end of pagination
    page.query_selector_all.side_effect = [[_card(href="/jobs/1")], []]

    jobs = browser_mod._fetch_url_pages(page, _FakeProfile(), CFG)

    assert len(jobs) == 1
    assert jobs[0].title == "Backend Engineer"


def test_a_later_page_timeout_is_still_just_the_end_of_pagination():
    """The other half of the distinction - this must NOT raise, or every
    finite listing would report a failure on its last page."""
    page = MagicMock()
    calls = {"n": 0}

    def wait(selector, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] > 1:          # page 2 onwards: no such page
            raise TimeoutError("no page 2")
        return MagicMock()

    page.wait_for_selector.side_effect = wait
    page.query_selector_all.return_value = [_card(href="/jobs/1")]

    jobs = browser_mod._fetch_url_pages(page, _FakeProfile(), CFG)

    assert len(jobs) == 1           # page 1's job, no exception


def test_the_two_timeouts_differ_and_are_profile_overridable():
    """A heavy careers page needs a longer first-page budget than a static
    one; hard-coding the worst case would slow every company's failure path
    to the slowest company's."""
    first, later = browser_mod._selector_timeouts(CFG)
    assert first > later, "the first page must get the more generous budget"

    tuned = dict(CFG, pagination=dict(CFG["pagination"],
                                      first_page_timeout_ms=45000,
                                      next_page_timeout_ms=3000))
    assert browser_mod._selector_timeouts(tuned) == (45000, 3000)


def test_non_israeli_locations_are_still_filtered_out():
    """The fix must not have widened what counts as relevant."""
    page = MagicMock()
    page.wait_for_selector.return_value = MagicMock()
    page.query_selector_all.side_effect = [
        [_card(location="Tel Aviv", href="/jobs/1"),
         _card(location="Austin, USA", href="/jobs/2")],
        [],
    ]

    jobs = browser_mod._fetch_url_pages(page, _FakeProfile(), CFG)

    assert [j.url for j in jobs] == ["https://careers.example.com/jobs/1"]
