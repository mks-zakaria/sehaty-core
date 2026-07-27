"""Slugs for place names (cities and districts).

Doctors enter their city and neighbourhood as free display text — "Casablanca",
"Maârif", "Aïn Diab". The public site browses them by URL segment, so the two
representations have to agree: ``/casablanca/maarif/dentiste`` must find the
doctor whose district reads "Maârif".

Rather than denormalize a second slug column (which then drifts from the display
value), slugs are derived on read and resolved back against the small set of
distinct place names actually present. There are tens of cities, not millions.

Accents are folded rather than dropped: without NFKD, "Maârif" would slugify to
``ma-rif`` — a URL nobody types and Google never matches.
"""

import re
import unicodedata


def place_slug(name: str) -> str:
    """URL segment for a place name: ``"Aïn Diab"`` -> ``"ain-diab"``.

    Folds accents to ASCII, lowercases, collapses every run of non-alphanumeric
    characters to a single dash, and trims dashes from the ends.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")


def match_display_names(slug: str, candidates: list[str | None]) -> list[str]:
    """Every candidate display name whose slug equals ``slug``.

    Returns a list, not a single value, because the same place is often stored
    with different spellings or casing ("Casablanca", "casablanca ", "CASABLANCA")
    and a browse page must show all of those doctors, not an arbitrary one.
    """
    target = place_slug(slug)
    return [name for name in candidates if name and place_slug(name) == target]
