import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from profiles import load_profile


def _fake_response(json_data):
    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


def test_lever_end_to_end_with_mocked_response():
    from fetchers import api as api_mod

    profile = load_profile(Path(__file__).resolve().parent.parent
                           / "profiles" / "mobileye.json")

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

    profile = load_profile(Path(__file__).resolve().parent.parent
                           / "profiles" / "mobileye.json")
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

    profile = load_profile(Path(__file__).resolve().parent.parent
                           / "profiles" / "wiz.json")
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


def test_greenhouse_multiple_locations_resolved_via_offices():
    from fetchers import api as api_mod

    profile = load_profile(Path(__file__).resolve().parent.parent
                           / "profiles" / "wiz.json")

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
