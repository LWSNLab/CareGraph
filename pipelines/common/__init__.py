"""Helpers shared across the ingestion stages.

Anything used by more than one of `parsers`, `scrapers`, `geocoding` or `load`
belongs here — not copied into each. The federal-state list is the cautionary
example: it briefly existed twice (in the OSM scraper and in the normaliser)
with no mechanism to keep the copies in step.

Deliberately named `common` rather than `utils`: a module that means
"miscellaneous" attracts miscellany. If something here grows its own identity,
give it a real home instead.
"""

from pipelines.common.normalize import BUNDESLAENDER, parse_bundeslaender

__all__ = ["BUNDESLAENDER", "parse_bundeslaender"]
