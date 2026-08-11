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
