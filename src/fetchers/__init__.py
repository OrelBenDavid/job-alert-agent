# -*- coding: utf-8 -*-
"""
The one dispatcher in the project: fetch_type -> module. That's all there
is - no slug->function mapping and no companies.json. A new company on an
existing platform/strategy doesn't touch this file at all, it only adds
profiles/<slug>.json.
"""

from models import Job
from . import api as _api
from . import html as _html
from . import browser as _browser

_FETCH_TYPE_DISPATCH = {
    "api": _api.fetch,
    "html": _html.fetch,
    "playwright": _browser.fetch,
}


def fetch_jobs(profile) -> list[Job]:
    """The only entry point meant to be called from outside (run.py).
    profile is a src.profiles.Profile. Raises NotImplementedError if
    fetch_type is unknown - shouldn't happen in practice since
    profiles.py already validates this at load time, but the double
    check is cheap and prevents odd silent behavior if something changed
    between the two stages."""
    handler = _FETCH_TYPE_DISPATCH.get(profile.fetch_type)
    if handler is None:
        raise NotImplementedError(
            f"{profile.slug}: unknown fetch_type: {profile.fetch_type!r}")
    return handler(profile)
