# -*- coding: utf-8 -*-
"""
Job-family classification: is this posting even in a field the user works in.

*** Why this exists ***

Until 2026-08-18 the bot had no notion of WHICH jobs the user wanted. It knew
one thing - "not more than N years of experience" - and delivered everything
else that was Israeli and not senior-titled. Measured across a live snapshot
of all 145 boards, ~47% of what survived to an alert was outside software,
hardware, data and IT entirely: Bookkeeper, Payroll Accountant, General
Counsel, Securities Sales Representative, Warehouse Clerk, Marketing Admin.

The target families were set by the user on 2026-08-18 and are exactly four:
software/data/ML engineering, hardware/VLSI/embedded, data analyst/BI/product,
and IT/technical support.

*** Why a BLOCKLIST and not an allowlist ***

An allowlist is the obvious design and it is the wrong one here. The corpus
contains real, on-target roles whose titles name no recognisable technology at
all - "DFIR", "CyOps Analyst", "InfoSec & SecOps", "Junior Intelligence
Analyst", "System Integrator", "Quality Assurance". An allowlist drops every
one of them silently, and state is written BEFORE filtering (see filters.py),
so a dropped job is unrecoverable - there is no replay by design.

So: a title matching a blocked domain is rejected, a title matching a target
family passes, and a title matching NEITHER is passed and flagged. That last
bucket is ~15% of postings and it is the price of not losing anything.

*** Why ALLOW_OVERRIDE has to exist ***

The blocklist keys on domain words, and a domain word inside an engineering
title does not make it a business role. "Support Engineer" is IT support (a
target family) and contains "support"; "Sales Engineer" is a sales role and
contains "engineer". Order alone can't separate them, so the override list
carries the unambiguous engineering nouns that win a title back, and stays
deliberately narrow - a generic "engineer" is NOT in it, which is exactly why
"Sales Engineer" is still rejected.

Hebrew terms are DATA, not comments - Israeli boards post in Hebrew - and stay
Hebrew regardless of the project's English-comments convention, the same rule
relevance.py follows.
"""

from __future__ import annotations   # see models.py - `X | None` on 3.9 too

import re
from typing import Literal

# Domains the user does not work in. Checked FIRST: a sales or finance role is
# still one even when its title says "engineer".
BLOCKED_DOMAINS = [
    # --- finance, legal, procurement ---
    "finance", "financial", "accountant", "accounting", "bookkeeper",
    "bookkeeping", "payroll", "controller", "comptroller", "tax", "treasury",
    "audit", "auditor", "billing", "collections", "credit", "invoicing",
    "procurement", "purchasing", "legal", "counsel", "paralegal",
    "compliance officer", "fp a", "investor relations", "underwriting",
    # --- sales, customer success, marketing, growth ---
    "sales", "seller", "account executive", "account manager",
    "business development", "bdr", "sdr", "customer success",
    "customer experience", "channel manager", "channel sales", "partnerships",
    "partner manager", "revenue", "marketing", "marketer", "brand",
    "content writer", "copywriter", "content creator", "social media", "seo",
    "aso", "demand generation", "growth", "community manager",
    "public relations", "campaign", "crm administrator", "pre sales",
    "presales", "solution engineer", "solutions engineer", "solution architect",
    "solutions architect", "field engineer", "sales administrator",
    # --- HR, recruiting, office, admin ---
    "recruiter", "recruiting", "recruitment", "talent acquisition",
    "human resources", "hris", "people partner", "people operations",
    "people ops", "welfare", "office manager", "administrative assistant",
    "executive assistant", "executive administrator", "personal assistant",
    "reception", "receptionist", "workplace experience", "employer brand",
    "learning and development", "organizational development",
    # --- manual, logistics, facilities ---
    "warehouse", "logistics", "shipping", "courier", "driver", "forklift",
    "packer", "cleaner", "facilities", "security guard", "cook", "chef",
    "barista", "cashier", "taxi", "vendor scheduler", "order management",
    "supply chain", "inventory", "production employee",
    # --- Hebrew ---
    "מכירות", "משאבי אנוש", "שיווק", "הנהלת חשבונות", "גיוס", "רכש",
    "מחסנאי", "נהג", "מלגזן", "אורז", "קופאי", "מוקדן", "סוכן מכירות",
]

# Wins a title BACK from the blocklist. Only unambiguous engineering nouns -
# a bare "engineer" is deliberately absent, so "Sales Engineer" stays blocked
# while "Support Engineer" does not.
TECH_OVERRIDES = [
    "software engineer", "software developer", "backend", "back end",
    "frontend", "front end", "full stack", "fullstack", "devops", "sre",
    "site reliability", "data engineer", "data scientist", "machine learning",
    "mlops", "algorithm", "embedded", "firmware", "vlsi", "asic", "fpga",
    "qa engineer", "automation engineer", "security researcher", "malware",
    "reverse engineer", "penetration test", "infrastructure engineer",
    "platform engineer", "system engineer", "systems engineer",
    "system administrator", "systems administrator", "sysadmin", "help desk",
    "helpdesk", "it support", "technical support", "support engineer",
    "network engineer", "database", "dba", "cloud engineer", "data analyst",
]

# Confirms a target family. A title matching none of the three lists is
# UNKNOWN, which passes and is flagged.
TARGET_FAMILIES = [
    # software / data / ML
    "engineer", "engineering", "developer", "development", "programmer",
    "software", "devops", "sre", "algorithm", "data scien", "data engineer",
    "machine learning", "deep learning", "computer vision", "nlp", "llm",
    "genai", "mlops", "research", "researcher", "automation", "tester", "qa",
    "quality assurance", "quality control", "sdet",
    # hardware / embedded
    "embedded", "firmware", "hardware", "vlsi", "asic", "fpga", "silicon",
    "physical design", "verification", "electro", "electrical", "analog",
    "signal", "optic", "mechanical", "rf",
    # security
    "cyber", "security", "infosec", "secops", "devsecops", "penetration",
    "malware", "vulnerability", "threat",
    # infra / IT
    "infrastructure", "platform", "cloud", "kubernetes", "network",
    "database", "dba", "system administrator", "sysadmin", "it specialist",
    "support", "help desk", "helpdesk", "technician", "integrator",
    # data / BI / product
    "analyst", "analytics", "business intelligence", "bi developer",
    "data platform", "product manager", "product owner", "product designer",
    "ux", "ui", "designer", "technical writer",
    # early-career markers - a strong signal on their own
    "student", "intern", "graduate", "trainee",
    # Hebrew
    "מפתח", "מפתחת", "מהנדס", "מהנדסת", "תוכנה", "אנליסט", "אנליסטית",
    "בודק תוכנה", "סטודנט", "מתמחה",
]

# Evergreen cards that are not a posting at all. Every board has one or two
# and they match no filter, so without this they arrive on every run they are
# re-detected.
NON_JOB_MARKERS = [
    "didnt find", "did not find", "explore new opportunities",
    "general application", "career at", "surprise us", "we always look",
    "join our talent", "talent pool", "talent community", "open application",
    "spontaneous application", "future opportunities", "other positions",
    "cant find", "לא מצאת", "משרה כללית",
]

# Temporary / maternity-cover roles. NEVER a rejection - they are real entry
# points for a junior candidate (Wix's "QA Engineer (Temp position)") - only
# a label, so the batch stays skimmable.
TEMPORARY_MARKERS = [
    "maternity", "temporary", "temp position", "replacement", "student rule",
    "fixed term", "contractor", "months)", "חופשת לידה", "זמני", "זמנית",
]

Classification = Literal["target", "blocked", "unknown"]


def _normalize(title: str) -> str:
    """Lowercase, apostrophes deleted, punctuation to spaces, space-padded for
    whole-word lookup.

    Whole-word rather than substring, for the same reason as
    filters._normalize_title: "sales" sits inside "Salesforce", and blocking
    "Salesforce Developer" as a sales role is exactly the quiet false reject
    this module must not produce.

    The apostrophe is DELETED rather than turned into a space - the same rule,
    and the same reason, as relevance._normalize. "Didn't find what you were
    looking for?" would otherwise normalize to "didn t find" and slip past the
    non-job markers, which is how it was caught."""
    text = re.sub(r"['’׳]", "", (title or "").lower())
    text = re.sub(r"[^0-9a-z֐-׿]+", " ", text)
    return " " + re.sub(r"\s+", " ", text).strip() + " "


def _matches(normalized: str, terms: list[str]) -> str | None:
    """The first term present as a whole word, or None."""
    return next((t for t in terms if f" {t} " in normalized), None)


def classify(title: str) -> tuple[Classification, str | None]:
    """(classification, the term that decided it).

    "unknown" is a real answer, not a failure: it means no list recognised the
    title, and the caller passes it through flagged."""
    normalized = _normalize(title)

    override = _matches(normalized, TECH_OVERRIDES)
    blocked = _matches(normalized, BLOCKED_DOMAINS)
    if blocked and not override:
        return "blocked", blocked

    hit = override or _matches(normalized, TARGET_FAMILIES)
    return ("target", hit) if hit else ("unknown", None)


def is_non_job(title: str) -> bool:
    """An evergreen "we're always hiring" card rather than a posting."""
    return _matches(_normalize(title), NON_JOB_MARKERS) is not None


def is_temporary(title: str) -> bool:
    """A temp/maternity-cover role. Tagged, never rejected."""
    return _matches(_normalize(title), TEMPORARY_MARKERS) is not None
