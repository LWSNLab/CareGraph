"""Shared fixtures for the pipeline tests.

**`seeded_providers` exists because the same mistake was made three times.**
Integration tests that assert against whatever the database happens to hold pass
on a developer machine — which is loaded — and fail in CI, where the schema is
built from the migrations and holds nothing. Each time the symptom looked
different (an empty dataset export, an empty search index) and the cause was the
same: a test depending on state it did not create.

Use this fixture rather than reaching for the ambient database. It seeds through
the real loader, so the rows go in exactly as ingestion would write them, and it
removes them afterwards without touching anything else.
"""

from __future__ import annotations

import os

import pytest

DSN = os.environ.get("CAREGRAPH_TEST_DSN")

# One namespace for every seeded row, so cleanup can never catch real data.
SEED_PREFIX = "test:seed:"


@pytest.fixture
def seeded_providers():
    """Insert five providers through the loader and yield the records.

    Rows are keyed `test:seed:<n>` and purged before and after: before, because a
    crashed earlier run must not make the next one fail; after, so a developer's
    loaded database is left as it was found.
    """
    if not DSN:
        pytest.skip("CAREGRAPH_TEST_DSN not set")

    import psycopg

    from pipelines.load.postgres_loader import PostgresLoader

    def purge():
        with psycopg.connect(DSN) as conn:
            conn.execute(
                "DELETE FROM care_infrastructure WHERE source_id LIKE %s", (SEED_PREFIX + "%",)
            )
            conn.commit()

    records = [
        {
            "source_id": f"{SEED_PREFIX}{n}",
            "type": "pflegeheim_stationaer",
            "name": f"PyTest Seed Heim {n}",
            # Alternating so tests can assert that an absent value stays absent
            # rather than becoming an empty string.
            "strasse": "Teststraße 1" if n % 2 else None,
            "plz": "10115" if n % 2 else None,
            "ort": "Berlin" if n % 2 else None,
            "bundesland": "Berlin",
            "website": "example.de" if n % 2 else None,
            "details": {"source": "test", "attribution": "test"},
            "lat": 52.5 + n / 1000,
            "lon": 13.4 + n / 1000,
        }
        for n in range(5)
    ]

    purge()
    PostgresLoader(DSN).load_providers(records)
    yield records
    purge()
