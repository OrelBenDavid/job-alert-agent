# -*- coding: utf-8 -*-
"""
Job link canonicalization - the basis for a stable id when the ATS doesn't
provide one of its own (HTML/Playwright). Identical in spirit to what's
documented in the career-site-profiler skill (assets/function_templates.py,
URL_HELPER) - kept as a single source of truth on purpose, not duplicated
between the project and the skill: if this drifts from the skill's copy,
that's a sign the skill and the code went out of sync and someone forgot to
update one of them.
"""

from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

# Params that change between runs and aren't part of the job's identity -
# must be stripped before a link becomes an id, otherwise every run would
# look like every job changed.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid", "source", "src", "ref", "referrer", "lever-source",
    "fbclid", "gclid", "_ga", "session", "sessionid", "t", "ts",
}


def canonicalize_url(href: str, base: str = "") -> str:
    """Normalizes a job link into a stable form to use as an id, and as the
    display url.

    Three real fixes, not cosmetics:
    1) relative -> absolute: otherwise the same job gets a different id if
       the site alternates between "/jobs/123" and "https://.../jobs/123"
       across renders.
    2) strip tracking params: some sites add them dynamically on every load.
    3) normalize trailing slash and drop the fragment: meaningless
       differences in the job's identity.
    """
    if not href:
        return ""
    absolute = urljoin(base, href.strip())
    parts = urlparse(absolute)
    kept = [(k, v) for k, v in parse_qsl(parts.query)
            if k.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunparse((parts.scheme, parts.netloc, path, "", urlencode(kept), ""))
