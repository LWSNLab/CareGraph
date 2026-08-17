"""Helpers shared across the ingestion stages.

Anything used by more than one of `parsers`, `scrapers`, `geocoding` or `load`
belongs here — not copied into each. The federal-state list is the cautionary
example: it briefly existed twice (in the OSM scraper and in the normaliser)
with no mechanism to keep the copies in step.

Deliberately named `common` rather than `utils`: a module that means
"miscellaneous" attracts miscellany. If something here grows its own identity,
give it a real home instead.
"""

from pipelines.common.dsn import DSNError, checked_dsn, dsn_from_env
from pipelines.common.normalize import (
    BUNDESLAENDER,
    HTTP_ONLY_HOSTS,
    normalize_website,
    parse_bundeslaender,
)
from pipelines.common.paths import DATA_DIR, PACKAGE_ROOT, PROCESSED_DIR, RAW_DIR
from pipelines.common.trust import use_system_trust_store

__all__ = [
    "BUNDESLAENDER",
    "DATA_DIR",
    "HTTP_ONLY_HOSTS",
    "PACKAGE_ROOT",
    "PROCESSED_DIR",
    "RAW_DIR",
    "DSNError",
    "checked_dsn",
    "dsn_from_env",
    "normalize_website",
    "parse_bundeslaender",
    "use_system_trust_store",
]
