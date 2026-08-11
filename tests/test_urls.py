from urls import canonicalize_url


def test_relative_and_absolute_converge():
    base = "https://careers.wix.com/positions"
    a = canonicalize_url("/jobs/123", base)
    b = canonicalize_url("https://careers.wix.com/jobs/123", base)
    assert a == b == "https://careers.wix.com/jobs/123"


def test_tracking_params_stripped():
    a = canonicalize_url("https://x.com/jobs/1?utm_source=li&gh_src=abc")
    assert a == "https://x.com/jobs/1"


def test_real_query_param_kept():
    a = canonicalize_url("https://x.com/jobs/1?dept=eng&utm_medium=social")
    assert a == "https://x.com/jobs/1?dept=eng"


def test_fragment_and_trailing_slash_ignored():
    a = canonicalize_url("https://x.com/jobs/1/#apply")
    b = canonicalize_url("https://x.com/jobs/1")
    assert a == b


def test_empty_href_returns_empty():
    assert canonicalize_url("") == ""
