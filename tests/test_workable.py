# -*- coding: utf-8 -*-
"""
The Workable fetcher, and specifically the thing that would have shipped broken.

Workable repeats a multi-location opening once per place, every copy carrying
the SAME shortcode. Measured live 2026-08-19: Plateful returns 24 items for 6
real openings, CXG 1,396 for 539, Nuvei 73 for 64 - 9 of the 16 importable
companies do it, so it is the platform's normal behaviour.

That is worse than the per-city duplicate defect already fixed for Comeet,
because these copies share an id and state is written BEFORE notification: one
opening would be recorded and alerted several times over, and the second alert
would look exactly like a real posting.
"""

from types import SimpleNamespace

from fetchers.api import fetch_workable

_FIELDS = {
    "id": "shortcode",
    "title": "title",
    "location": "country",
    "url": "url",
    "location_city": "city",
    "location_state": "state",
    "locations": "locations",
    "location_country_code": "countryCode",
}


def _profile(detail_fetch=None):
    return SimpleNamespace(
        slug="acme",
        detail_fetch=detail_fetch,
        raw={"api": {"endpoint": "https://x/api", "fields": _FIELDS,
                     "platform": "workable"}},
    )


def _posting(shortcode, title, city="", state="", country="", code=None,
             url="https://apply.workable.com/j/X"):
    return {
        "shortcode": shortcode, "title": title, "url": url,
        "city": city, "state": state, "country": country,
        "locations": [{"country": country, "countryCode": code,
                       "city": city, "region": state, "hidden": False}],
    }


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fetch(monkeypatch, jobs, profile=None):
    monkeypatch.setattr("fetchers.api.requests.get",
                        lambda *a, **k: _Response({"jobs": jobs}))
    return fetch_workable(profile or _profile())


# ---------------------------------------------------------------------------
# The per-location duplication
# ---------------------------------------------------------------------------

def test_one_opening_in_six_countries_is_one_job(monkeypatch):
    """The Plateful shape, exactly: one shortcode, six location records."""
    jobs = [_posting("B6C81D929E", "Sales Closer", country=c, code=k)
            for c, k in [("Israel", "IL"), ("Argentina", "AR"),
                         ("Colombia", "CO"), ("Costa Rica", "CR"),
                         ("Nicaragua", "NI"), ("Honduras", "HN")]]
    out = _fetch(monkeypatch, jobs)
    assert len(out) == 1
    assert out[0].id == "B6C81D929E"


def test_the_israeli_location_is_the_one_displayed(monkeypatch):
    """A collapsed group must not show whichever copy happened to be first -
    the alert has to say where the job the user can take actually is."""
    jobs = [
        _posting("A1", "Backend Engineer", country="Poland", code="PL"),
        _posting("A1", "Backend Engineer", city="Tel Aviv-Yafo",
                 state="Tel Aviv District", country="Israel", code="IL"),
    ]
    out = _fetch(monkeypatch, jobs)
    assert len(out) == 1
    assert out[0].location == "Tel Aviv-Yafo, Tel Aviv District"


def test_a_group_survives_if_any_location_is_israeli(monkeypatch):
    """Fail-open in the same direction as Ashby's secondaryLocations: a posting
    open in several places is relevant if one of them is here."""
    jobs = [
        _posting("A1", "QA Engineer", country="United States", code="US"),
        _posting("A1", "QA Engineer", city="Haifa", country="Israel", code="IL"),
    ]
    assert len(_fetch(monkeypatch, jobs)) == 1


def test_distinct_shortcodes_are_not_collapsed(monkeypatch):
    """The collapse must key on the id, not on the title - two real openings
    can share a title across teams."""
    jobs = [_posting("A1", "Backend Engineer", city="Tel Aviv",
                     country="Israel", code="IL"),
            _posting("A2", "Backend Engineer", city="Haifa",
                     country="Israel", code="IL")]
    assert len({j.id for j in _fetch(monkeypatch, jobs)}) == 2


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------

def test_the_country_code_decides_israel_outright(monkeypatch):
    """countryCode is a picker value, so it is trusted the way Comeet's
    location.country is - and read from every entry, not just the first."""
    jobs = [_posting("A1", "Data Engineer", city="", country="", code="IL")]
    assert len(_fetch(monkeypatch, jobs)) == 1


def test_a_foreign_country_code_rejects_nobody_on_its_own(monkeypatch):
    """is_israel_country_code is ADDITIVE by design. A foreign code must not
    become a reason to drop a posting - that inversion is what would silently
    lose qualified remote."""
    jobs = [_posting("A1", "Backend Engineer", country="Remote", code="US")]
    out = _fetch(monkeypatch, jobs)
    assert len(out) == 1          # kept as qualified remote, not rejected


def test_a_foreign_city_is_dropped(monkeypatch):
    jobs = [_posting("A1", "Backend Engineer", city="Berlin",
                     country="Germany", code="DE")]
    assert _fetch(monkeypatch, jobs) == []


def test_a_remote_role_naming_a_foreign_country_is_dropped(monkeypatch):
    """The join of city/state/country is what makes this visible: 'Remote' and
    'United States' arrive in different fields and must be judged together."""
    jobs = [_posting("A1", "Support Engineer", city="Remote",
                     country="United States", code="US")]
    assert _fetch(monkeypatch, jobs) == []


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_the_stable_id_is_the_shortcode_not_the_url(monkeypatch):
    """A link can be rewritten by a title change; the shortcode is what
    /j/{shortcode} resolves to. Same rule as Comeet's uid."""
    jobs = [_posting("144092745E", "Data Analyst", city="Tel Aviv-Yafo",
                     country="Israel", code="IL",
                     url="https://apply.workable.com/j/144092745E")]
    assert _fetch(monkeypatch, jobs)[0].id == "144092745E"


def test_the_description_is_taken_inline(monkeypatch):
    """?details=true carries the body in the listing, which is what keeps
    Workable clear of MAX_DETAIL_FETCHES_PER_RUN."""
    job = _posting("A1", "Backend Engineer", city="Tel Aviv",
                   country="Israel", code="IL")
    job["description"] = "<p>We need <b>3 years</b> of experience.</p>"
    profile = _profile(detail_fetch={"method": "inline",
                                     "inline_field": "description",
                                     "content_is_html": True})
    out = _fetch(monkeypatch, [job], profile)
    assert "3 years" in out[0].description


def test_an_empty_board_is_no_jobs_not_an_error(monkeypatch):
    """9 of the 38 companies in Workable's Israel feed serve an empty account
    board. That must read as zero jobs and reach the health gate, not raise."""
    assert _fetch(monkeypatch, []) == []
