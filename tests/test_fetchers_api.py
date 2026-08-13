import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from profiles import find_profile_path, load_profile


def _fake_response(json_data):
    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


def test_lever_end_to_end_with_mocked_response():
    from fetchers import api as api_mod

    profile = load_profile(find_profile_path("mobileye"))

    fake_postings = [
        {"id": "abc123", "text": "Backend Engineer",
         "categories": {"location": "Tel-Aviv, Israel"}, "hostedUrl": "https://x/1"},
        {"id": "def456", "text": "Sales Rep (US)",
         "categories": {"location": "Austin, USA"}, "hostedUrl": "https://x/2"},
        {"id": "ghi789", "text": "QA Engineer",
         "categories": {"location": "Jerusalem, Israel"}, "hostedUrl": "https://x/3"},
    ]

    with patch.object(api_mod.requests, "get",
                      return_value=_fake_response(fake_postings)):
        jobs = api_mod.fetch(profile)

    ids = {j.id for j in jobs}
    assert ids == {"abc123", "ghi789"}         # the US role was filtered, both Israel ones kept
    assert all(j.company == "mobileye" for j in jobs)
    assert all(j.url.startswith("https://x/") for j in jobs)


def test_lever_populates_description_from_the_structured_lists_field():
    """Against the REAL mobileye profile: the listing already carries the
    requirements, so no per-posting request is ever built. The shape here is
    the one verified live - sections with recruiter-written headings and bare
    <li> runs."""
    from fetchers import api as api_mod
    from experience import read_experience

    profile = load_profile(find_profile_path("mobileye"))
    assert profile.detail_fetch["inline_field"] == "lists"

    fake_postings = [{
        "id": "abc123", "text": "3D Algorithm Developer",
        "categories": {"location": "Ramat Gan, Israel"},
        "hostedUrl": "https://jobs.eu.lever.co/mobileye/abc123",
        "description": "<p>Mobileye is looking for a 3D Algorithm Developer.</p>",
        "lists": [
            {"text": "All you need is:",
             "content": "<li>2+ years of experience with C++</li>"},
            {"text": "Nice to have:",
             "content": "<li>10+ years of experience with CUDA</li>"},
        ],
    }]

    with patch.object(api_mod.requests, "get",
                      return_value=_fake_response(fake_postings)):
        jobs = api_mod.fetch(profile)

    assert len(jobs) == 1
    assert jobs[0].description is not None
    # 2+ from the requirements section, NOT 10+ from the nice-to-have one.
    assert read_experience(jobs[0].description).min_years == 2.0


def test_greenhouse_inline_content_is_unescaped_by_the_fetcher():
    """Against the REAL wiz profile: Greenhouse's content arrives both as
    HTML and HTML-escaped, and the fetcher must undo the escaping."""
    from fetchers import api as api_mod
    from experience import read_experience

    profile = load_profile(find_profile_path("wiz"))
    assert profile.detail_fetch["inline_field"] == "content"

    fake_data = {"jobs": [{
        "id": 1, "title": "Backend Engineer",
        "location": {"name": "Tel Aviv"}, "offices": [{"name": "Tel Aviv"}],
        "absolute_url": "https://x/1",
        "content": ("&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;"
                    "&lt;li&gt;1 year of experience&lt;/li&gt;&lt;/ul&gt;"),
    }]}

    with patch.object(api_mod.requests, "get",
                      return_value=_fake_response(fake_data)):
        jobs = api_mod.fetch(profile)

    assert "&lt;" not in jobs[0].description
    assert read_experience(jobs[0].description).min_years == 1.0


def _ashby_profile(tmp_path, **api_overrides):
    """A minimal Ashby profile on disk. Built here rather than pointed at a
    shipped profile because the two Ashby companies are imported in bulk in
    Phase 3, and a test should not depend on a generated file."""
    import json
    api = {"platform": "ashby",
           "endpoint": "https://api.ashbyhq.com/posting-api/job-board/acme",
           "fields": {"id": "id", "title": "title", "location": "location",
                      "url": "jobUrl"}}
    api.update(api_overrides)
    data = {
        "schema_version": 3, "slug": "acme", "name": "Acme", "enabled": True,
        "careers_url": "https://jobs.ashbyhq.com/acme", "fetch_type": "api",
        "israel_filter": {"method": "post_fetch"}, "api": api,
        "health": {"expected_min_jobs": 1}, "verified_on": "2026-08-13",
    }
    path = tmp_path / "acme.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_profile(path)


def test_ashby_filters_to_relevant_locations(tmp_path):
    """The shape verified live on 2026-08-13: a flat `jobs` array, `location`
    as a plain string, `jobUrl` absolute."""
    from fetchers import api as api_mod

    profile = _ashby_profile(tmp_path)
    fake = {"jobs": [
        {"id": "a-1", "title": "Backend Engineer", "location": "Tel Aviv",
         "secondaryLocations": [], "jobUrl": "https://jobs.ashbyhq.com/acme/a-1"},
        {"id": "a-2", "title": "Account Executive", "location": "New York",
         "secondaryLocations": [], "jobUrl": "https://jobs.ashbyhq.com/acme/a-2"},
        {"id": "a-3", "title": "Support Lead", "location": "EMEA Remote",
         "secondaryLocations": [], "jobUrl": "https://jobs.ashbyhq.com/acme/a-3"},
        {"id": "a-4", "title": "Sales Rep", "location": "US Remote",
         "secondaryLocations": [], "jobUrl": "https://jobs.ashbyhq.com/acme/a-4"},
    ], "apiVersion": 1}

    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    # Tel Aviv and the unqualified EMEA remote; not New York, not US Remote.
    assert {j.id for j in jobs} == {"a-1", "a-3"}
    assert all(j.company == "acme" for j in jobs)
    assert all(j.url.startswith("https://jobs.ashbyhq.com/acme/") for j in jobs)


def test_ashby_reads_secondary_locations_in_both_documented_shapes(tmp_path):
    """secondaryLocations was EMPTY on both live boards, so its element shape
    could not be verified - the fetcher accepts a dict and a bare string for
    that reason. This pins that tolerance so a later "tidy-up" doesn't quietly
    narrow it to whichever shape the author happened to assume."""
    from fetchers import api as api_mod

    profile = _ashby_profile(tmp_path, fields={
        "id": "id", "title": "title", "location": "location",
        "url": "jobUrl", "secondary_location": "secondaryLocations"})
    fake = {"jobs": [
        {"id": "b-1", "title": "Dict shape", "location": "Berlin",
         "secondaryLocations": [{"location": "Tel Aviv"}],
         "jobUrl": "https://jobs.ashbyhq.com/acme/b-1"},
        {"id": "b-2", "title": "String shape", "location": "Berlin",
         "secondaryLocations": ["Haifa"],
         "jobUrl": "https://jobs.ashbyhq.com/acme/b-2"},
        {"id": "b-3", "title": "No Israeli location anywhere",
         "location": "Berlin", "secondaryLocations": [{"location": "Paris"}],
         "jobUrl": "https://jobs.ashbyhq.com/acme/b-3"},
    ], "apiVersion": 1}

    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    assert {j.id for j in jobs} == {"b-1", "b-2"}


def test_ashby_ignores_secondary_locations_when_unmapped(tmp_path):
    """No `secondary_location` in the field map means the field is not read at
    all - a profile opts in. Guards the default staying off."""
    from fetchers import api as api_mod

    profile = _ashby_profile(tmp_path)
    fake = {"jobs": [
        {"id": "c-1", "title": "Engineer", "location": "Berlin",
         "secondaryLocations": [{"location": "Tel Aviv"}],
         "jobUrl": "https://jobs.ashbyhq.com/acme/c-1"},
    ], "apiVersion": 1}

    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    assert jobs == []


def test_ashby_populates_description_from_description_html(tmp_path):
    """Ashby returns real HTML in descriptionHtml - NOT HTML-escaped the way
    Greenhouse's `content` arrives, so it needs no unescaping pass."""
    from fetchers import api as api_mod
    from experience import read_experience

    import json
    profile = _ashby_profile(tmp_path)
    raw = json.loads((tmp_path / "acme.json").read_text(encoding="utf-8"))
    raw["detail_fetch"] = {
        "method": "inline", "inline_field": "descriptionHtml",
        "content_is_html": True,
        "verified_on_job_url": "https://jobs.ashbyhq.com/acme/a-1"}
    (tmp_path / "acme.json").write_text(json.dumps(raw), encoding="utf-8")
    profile = load_profile(tmp_path / "acme.json")

    fake = {"jobs": [{
        "id": "a-1", "title": "Backend Engineer", "location": "Tel Aviv",
        "secondaryLocations": [], "jobUrl": "https://jobs.ashbyhq.com/acme/a-1",
        "descriptionHtml": "<h3>Requirements</h3><ul><li>4+ years of experience"
                           " in backend development</li></ul>",
    }], "apiVersion": 1}

    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    assert jobs[0].description is not None
    assert read_experience(jobs[0].description).min_years == 4.0


def test_greenhouse_multiple_locations_resolved_via_offices():
    from fetchers import api as api_mod

    profile = load_profile(find_profile_path("wiz"))

    fake_data = {"jobs": [
        {"id": 1, "title": "Cloud Engineer",
         "location": {"name": "Multiple Locations"},
         "offices": [{"name": "Tel Aviv"}, {"name": "New York"}],
         "absolute_url": "https://x/1"},
        {"id": 2, "title": "Product Marketing",
         "location": {"name": "London, UK"}, "offices": [{"name": "London"}],
         "absolute_url": "https://x/2"},
    ]}

    with patch.object(api_mod.requests, "get",
                      return_value=_fake_response(fake_data)):
        jobs = api_mod.fetch(profile)

    ids = {j.id for j in jobs}
    assert ids == {"1"}   # only the job with a Tel Aviv office got through,
                          # despite the generic "Multiple Locations" name


# ---------------------------------------------------------------------------
# HiBob's own careers product
# ---------------------------------------------------------------------------

def test_hibob_sends_the_referer_the_endpoint_requires():
    """The endpoint returns 401 to a plain request and 200 with a Referer of
    the site's own origin. It is a soft anti-scraping check, not auth - but
    dropping the header turns every run into a fetch failure, so it is pinned
    here rather than left to a comment. The Referer is DERIVED from the
    endpoint so the two cannot drift apart."""
    from fetchers import api as api_mod

    profile = load_profile(find_profile_path("hibob"))
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers") or {}
        return _fake_response({"jobAdDetails": []})

    with patch.object(api_mod.requests, "get", side_effect=fake_get):
        api_mod.fetch(profile)

    assert captured["headers"].get("Referer") == \
        "https://hibob-fa0ad69d0cb34a.careers.hibob.com/"
    assert "Mozilla" in captured["headers"].get("User-Agent", "")


def test_hibob_filters_on_country_not_the_two_letter_site_code():
    """`site` carries 'IL' and `country` carries 'Israel'. Two-letter codes
    must never become relevance keywords - 'il' is far too short to match
    safely - so the profile points at country, and this pins that."""
    from fetchers import api as api_mod

    profile = load_profile(find_profile_path("hibob"))
    fake = {"jobAdDetails": [
        {"id": "u-1", "title": "Backend Engineer", "site": "IL",
         "country": "Israel", "requirements": "<li>3+ years</li>"},
        {"id": "u-2", "title": "Account Executive", "site": "US",
         "country": "United States", "requirements": "<li>5+ years</li>"},
        {"id": "u-3", "title": "Designer", "site": "UK",
         "country": "United Kingdom", "requirements": ""},
    ]}

    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    assert {j.id for j in jobs} == {"u-1"}
    assert jobs[0].location == "Israel"


def test_hibob_builds_the_job_url_from_the_template():
    """The listing carries no per-posting link, only a UUID. A blank url would
    render as an unlinked bullet forever, so the template is mandatory."""
    from fetchers import api as api_mod

    profile = load_profile(find_profile_path("hibob"))
    fake = {"jobAdDetails": [
        {"id": "abc-123", "title": "QA Engineer", "site": "IL",
         "country": "Israel", "requirements": "<li>2+ years</li>"},
    ]}

    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    assert jobs[0].url == (
        "https://hibob-fa0ad69d0cb34a.careers.hibob.com/jobs/abc-123")


def test_hibob_reads_requirements_not_the_marketing_description():
    """SKILL Step 3c.1: `description` holds company boilerplate and the role
    summary; `requirements` holds the qualification bullets. Pointing at
    description would validate, return content on every posting, and determine
    nothing - the failure that looks exactly like success."""
    from fetchers import api as api_mod
    from experience import read_experience

    profile = load_profile(find_profile_path("hibob"))
    assert profile.detail_fetch["inline_field"] == "requirements"

    fake = {"jobAdDetails": [{
        "id": "u-1", "title": "Backend Engineer", "site": "IL",
        "country": "Israel",
        "description": "<p>HiBob helps modern businesses. Founded 2015.</p>",
        "requirements": "<ul><li>6+ years of backend experience</li></ul>",
    }]}

    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    # 6 comes from `requirements`. The description names a year (2015) and
    # must not be read at all.
    assert read_experience(jobs[0].description).min_years == 6.0


def test_hibob_prepends_the_requirements_heading_the_parser_needs():
    """inline_prefix, and it is load-bearing rather than cosmetic.

    experience.classify_block decides what a bullet means from the heading
    above it, and HiBob's `requirements` field is bare "<ul><li>…" with no
    heading - so its bullets were never classified as requirements. Measured
    live across 17 Israel-relevant postings: 2 yielded a number bare, 17
    yielded one behind a synthetic "<h3>Requirements</h3>". This asserts the
    prefix is actually applied, because without it the field looks populated,
    parses cleanly, and silently determines almost nothing."""
    from fetchers import api as api_mod

    profile = load_profile(find_profile_path("hibob"))
    assert profile.detail_fetch["inline_prefix"] == "<h3>Requirements</h3>"

    fake = {"jobAdDetails": [{
        "id": "u-1", "title": "Backend Engineer", "site": "IL",
        "country": "Israel",
        "requirements": "<ul><li>3-5 years of experience in automation</li></ul>",
    }]}

    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    assert jobs[0].description.startswith("<h3>Requirements</h3>")


def test_inline_prefix_is_off_unless_a_profile_asks_for_it(tmp_path):
    """It must stay opt-in. Prepending a requirements heading to a field that
    is NOT requirements would promote nice-to-haves into hard requirements and
    start suppressing jobs the user should see."""
    from fetchers import api as api_mod

    profile = load_profile(find_profile_path("wiz"))   # greenhouse, no prefix
    assert "inline_prefix" not in (profile.detail_fetch or {})

    fake = {"jobs": [{
        "id": 1, "title": "Backend Engineer", "location": {"name": "Tel Aviv"},
        "offices": [{"name": "Tel Aviv"}], "absolute_url": "https://x/1",
        "content": "<ul><li>4+ years</li></ul>",
    }]}
    with patch.object(api_mod.requests, "get", return_value=_fake_response(fake)):
        jobs = api_mod.fetch(profile)

    assert not jobs[0].description.startswith("<h3>")
