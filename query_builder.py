"""
query_builder.py - Search query generation for RFPScout.

Responsibilities:
  - Accept a sector and service type as inputs.
  - Return a deduplicated list of search query strings built from 6
    templates.
  - Normalise loose user input (e.g. "Marketing", "MARKETING COMMS")
    to a canonical service key before building queries.

Design decisions:
  - SERVICE_TERMS maps each canonical service key to two variants:
      "keyword"  = short term used in broad templates
      "phrase"   = longer phrase used in quoted/specific templates
    This avoids queries like 'nonprofit "marketing" RFP' (too broad) or
    'site:sam.gov "marketing communications agency services"' (too long).
  - SECTOR_ALIASES normalises common user inputs to canonical sector
    names without requiring exact spelling.
  - build_queries() always returns a plain list[str] — no objects, no
    dicts. searcher.py iterates over it directly.
  - Year defaults to the current calendar year. Including the year
    filters out stale RFPs from previous cycles without needing
    post-processing.
  - All 6 templates are always generated. Deduplication removes any
    accidental identical strings (unlikely but safe).
  - Input validation raises ValueError with a clear message so the
    interactive CLI in agent.py can catch it and re-prompt.
"""

from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Canonical sector and service definitions
# ---------------------------------------------------------------------------

# Canonical sector names. These are the values agent.py passes in.
SECTORS = {
    "arts",
    "education",
    "environment",
    "health",
    "human services",
    "housing",
    "advocacy",
    "faith-based",
}

# Aliases map common user inputs to canonical sector names.
# Keys are lowercase stripped strings.
SECTOR_ALIASES: dict[str, str] = {
    # arts
    "arts": "arts",
    "art": "arts",
    "culture": "arts",
    "arts and culture": "arts",
    # education
    "education": "education",
    "educational": "education",
    "schools": "education",
    # environment
    "environment": "environment",
    "environmental": "environment",
    "conservation": "environment",
    "climate": "environment",
    # health
    "health": "health",
    "healthcare": "health",
    "medical": "health",
    "public health": "health",
    # human services
    "human services": "human services",
    "social services": "human services",
    "community services": "human services",
    "social care": "human services",
    # housing
    "housing": "housing",
    "affordable housing": "housing",
    "homelessness": "housing",
    # advocacy
    "advocacy": "advocacy",
    "policy": "advocacy",
    "civic": "advocacy",
    # faith-based
    "faith-based": "faith-based",
    "faith based": "faith-based",
    "religious": "faith-based",
    "church": "faith-based",
}

# Each service key maps to:
#   keyword - short term for broader templates
#   phrase  - quoted phrase for specific templates
SERVICE_TERMS: dict[str, dict[str, str]] = {
    "marketing": {
        "keyword": "marketing",
        "phrase": "marketing agency",
    },
    "fundraising": {
        "keyword": "fundraising",
        "phrase": "fundraising consulting",
    },
    "technology": {
        "keyword": "technology",
        "phrase": "technology services",
    },
    "website": {
        "keyword": "website",
        "phrase": "website design",
    },
    "consulting": {
        "keyword": "consulting",
        "phrase": "strategic consulting",
    },
    "event": {
        "keyword": "event",
        "phrase": "event management",
    },
    "pr": {
        "keyword": "PR",
        "phrase": "PR agency",
    },
    "other": {
        "keyword": "agency services",
        "phrase": "agency services",
    },
}

# Service aliases map user-friendly inputs to canonical service keys.
SERVICE_ALIASES: dict[str, str] = {
    # marketing
    "marketing": "marketing",
    "marketing agency": "marketing",
    "marketing communications": "marketing",
    "communications": "marketing",
    "digital marketing": "marketing",
    # fundraising
    "fundraising": "fundraising",
    "development": "fundraising",
    "fundraising / development": "fundraising",
    "fund development": "fundraising",
    # technology
    "technology": "technology",
    "tech": "technology",
    "crm": "technology",
    "technology / crm": "technology",
    "it": "technology",
    "database": "technology",
    # website
    "website": "website",
    "website design": "website",
    "web design": "website",
    "web development": "website",
    # consulting
    "consulting": "consulting",
    "strategic consulting": "consulting",
    "strategy": "consulting",
    # event
    "event": "event",
    "events": "event",
    "event management": "event",
    "event planning": "event",
    # pr
    "pr": "pr",
    "public relations": "pr",
    "media relations": "pr",
    "pr / media relations": "pr",
    # other
    "other": "other",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_queries(
    sector: str,
    service: str,
    year: Optional[int] = None,
) -> list[str]:
    """
    Build a deduplicated list of search query strings.

    Args:
        sector:  Nonprofit sector to target. See SECTORS for valid values.
                 Common aliases are also accepted (see SECTOR_ALIASES).
        service: Service type being procured. See SERVICE_TERMS for valid
                 keys. Common aliases are also accepted.
        year:    Calendar year to include in date-sensitive templates.
                 Defaults to the current year.

    Returns:
        A list of 6 unique query strings ready for searcher.py.

    Raises:
        ValueError: If sector or service cannot be resolved to a known
                    canonical value after alias lookup.
    """
    canonical_sector = _resolve_sector(sector)
    canonical_service = _resolve_service(service)
    resolved_year = year or datetime.now().year

    terms = SERVICE_TERMS[canonical_service]
    kw = terms["keyword"]        # short keyword form
    ph = terms["phrase"]         # quoted phrase form

    queries = [
        _t1(ph, resolved_year),
        _t2(kw, resolved_year),
        _t3(canonical_sector, kw),
        _t4(ph, resolved_year),
        _t5(canonical_sector, kw),
        _t6(canonical_sector),
    ]

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            unique.append(q)

    return unique


def list_sectors() -> list[str]:
    """Return sorted list of canonical sector names for CLI display."""
    return sorted(SECTORS)


def list_services() -> list[str]:
    """Return sorted list of canonical service keys for CLI display."""
    return sorted(SERVICE_TERMS.keys())


# ---------------------------------------------------------------------------
# Query templates
# ---------------------------------------------------------------------------
# Each template is a separate function so they are individually testable
# and easy to modify without touching the others.

def _t1(phrase: str, year: int) -> str:
    """
    Template 1 - Broad phrase + year.
    Targets pages that explicitly name the service and use RFP language.
    Example: nonprofit "marketing agency" "request for proposal" 2026
    """
    return f'nonprofit "{phrase}" "request for proposal" {year}'


def _t2(keyword: str, year: int) -> str:
    """
    Template 2 - .org site restriction + year.
    Biases toward nonprofit domains. Google CSE supports *.org in site:.
    Example: site:*.org "fundraising" RFP deadline 2026
    """
    return f'site:*.org "{keyword}" RFP deadline {year}'


def _t3(sector: str, keyword: str) -> str:
    """
    Template 3 - Sector + keyword + filetype:pdf.
    PDFs are more likely to be formal procurement documents than HTML pages.
    Example: "education" nonprofit RFP "website" filetype:pdf
    """
    return f'"{sector}" nonprofit RFP "{keyword}" filetype:pdf'


def _t4(phrase: str, year: int) -> str:
    """
    Template 4 - "proposals due" OR "RFP" variant.
    Catches pages that use "proposals due" instead of "request for proposal".
    Example: nonprofit "marketing agency" "proposals due" OR "RFP" 2026
    """
    return f'nonprofit "{phrase}" "proposals due" OR "RFP" {year}'


def _t5(sector: str, keyword: str) -> str:
    """
    Template 5 - sam.gov targeting.
    sam.gov lists federal and nonprofit procurement opportunities.
    Example: site:sam.gov "education" nonprofit "website"
    """
    return f'site:sam.gov "{sector}" nonprofit "{keyword}"'


def _t6(sector: str) -> str:
    """
    Template 6 - Sector-only broad sweep with negative filters.
    -government and -federal reduce false positives from public sector RFPs
    that are not relevant to the nonprofit-agency market.
    Example: "environment" "request for proposals" "agency" -government -federal
    """
    return f'"{sector}" "request for proposals" "agency" -government -federal'


# ---------------------------------------------------------------------------
# Input normalisation helpers
# ---------------------------------------------------------------------------

def _resolve_sector(raw: str) -> str:
    """
    Normalise a raw sector input to a canonical sector name.
    Raises ValueError if no match is found.
    """
    key = raw.strip().lower()
    resolved = SECTOR_ALIASES.get(key)
    if resolved:
        return resolved
    # Direct match against canonical set (catches exact canonical inputs)
    if key in SECTORS:
        return key
    raise ValueError(
        f"Unknown sector: '{raw}'. "
        f"Valid options: {', '.join(sorted(SECTORS))}. "
        f"Run list_sectors() to see all options."
    )


def _resolve_service(raw: str) -> str:
    """
    Normalise a raw service input to a canonical service key.
    Raises ValueError if no match is found.
    """
    key = raw.strip().lower()
    resolved = SERVICE_ALIASES.get(key)
    if resolved:
        return resolved
    # Direct match against canonical keys
    if key in SERVICE_TERMS:
        return key
    raise ValueError(
        f"Unknown service: '{raw}'. "
        f"Valid options: {', '.join(sorted(SERVICE_TERMS.keys()))}. "
        f"Run list_services() to see all options."
    )