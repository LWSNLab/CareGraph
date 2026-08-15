"""Tests for the distributable dataset (story E4-S5).

The point of the artefact is that someone who has never run the ingestion ends
up with the same data the API serves. These tests guard the two ways that can
quietly fail: an archive that does not say what it holds, and a CSV round trip
that changes values on the way through.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from pipelines.dataset.export import COLUMNS, export_dataset, latest_migration
from pipelines.dataset.load import import_dataset, read_archive
from pipelines.load.postgres_loader import PostgresLoader

DSN = os.environ.get("CAREGRAPH_TEST_DSN")
integration = pytest.mark.skipif(not DSN, reason="CAREGRAPH_TEST_DSN not set")


# ------------------------------------------------------------------- helpers


def build_archive(path: Path, rows: list[dict], **manifest_overrides) -> Path:
    """Write an archive by hand, so a test can make it wrong on purpose."""
    header = ",".join(COLUMNS)
    lines = [header]
    for row in rows:
        lines.append(",".join(str(row.get(c, "") or "") for c in COLUMNS))
    csv_text = "\n".join(lines) + "\n"

    manifest = {
        "name": "caregraph-providers",
        "generated_at": "2026-08-15",
        "row_count": len(rows),
        "schema_migration": latest_migration(Path("db/migrations")),
        "attribution": "© OpenStreetMap contributors (ODbL)",
    }
    manifest.update(manifest_overrides)

    with tarfile.open(path, "w:gz") as archive:
        for name, content in (
            ("providers.csv", csv_text),
            ("MANIFEST.json", json.dumps(manifest)),
            ("LICENSE.txt", "ODbL"),
            ("README.md", "# test"),
        ):
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def sample_row(**overrides) -> dict:
    row = {
        "source_id": "osm:node/1",
        "ik_nummer": "",
        "type": "pflegeheim_stationaer",
        "name": "PyTest Heim",
        "parent_organization": "",
        "website": "example.de",
        "strasse": "Teststraße 1",
        "plz": "10115",
        "ort": "Berlin",
        "bundesland": "Berlin",
        "details": '{"source": "test"}',
        "lat": "52.52",
        "lon": "13.405",
    }
    row.update(overrides)
    return row


# --------------------------------------------------------------- archive shape


def test_archive_must_declare_what_it_holds(tmp_path):
    """A truncated download must fail loudly, not import half a dataset."""
    path = build_archive(tmp_path / "a.tar.gz", [sample_row()], row_count=99)
    with pytest.raises(ValueError, match="claims 99 rows but holds 1"):
        read_archive(path)


def test_archive_missing_a_column_is_rejected(tmp_path):
    path = tmp_path / "b.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in (
            ("providers.csv", "source_id,name\nosm:node/1,X\n"),
            ("MANIFEST.json", json.dumps({"row_count": 1})),
        ):
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="missing columns"):
        read_archive(path)


def test_a_foreign_tarball_is_rejected(tmp_path):
    path = tmp_path / "c.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        data = b"nothing to see"
        info = tarfile.TarInfo("random.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="is this a CareGraph dataset"):
        read_archive(path)


# ------------------------------------------------------------ CSV round trip


def test_empty_cells_become_none_not_empty_strings(tmp_path):
    """CSV has no types. An absent street must stay absent.

    Writing "" would store an empty address rather than a missing one, and the
    two are not the same thing to a consumer deciding whether to show it.
    """
    row = sample_row(strasse="", plz="", ik_nummer="", parent_organization="")
    _, records = read_archive(build_archive(tmp_path / "d.tar.gz", [row]))

    record = records[0]
    for field in ("strasse", "plz", "ik_nummer", "parent_organization"):
        assert record[field] is None, f"{field} came back as {record[field]!r}"


def test_coordinates_and_details_survive_the_round_trip(tmp_path):
    _, records = read_archive(build_archive(tmp_path / "e.tar.gz", [sample_row()]))
    record = records[0]

    assert record["lat"] == pytest.approx(52.52)
    assert record["lon"] == pytest.approx(13.405)
    assert record["details"] == {"source": "test"}


def test_a_row_without_coordinates_is_not_forced_to_zero(tmp_path):
    """0/0 is a real place in the Gulf of Guinea, so a missing coordinate must
    stay missing rather than putting a German care home at Null Island."""
    _, records = read_archive(
        build_archive(tmp_path / "f.tar.gz", [sample_row(lat="", lon="")]))
    assert records[0]["lat"] is None
    assert records[0]["lon"] is None


# -------------------------------------------------------------- loader keying


def test_an_explicit_source_id_is_honoured():
    """The loader used to rebuild `osm:…` for every dict record.

    That hardcoded one source into a source-agnostic loader: re-importing an
    export, or loading the hospital directory, would have been rekeyed as if it
    came from OpenStreetMap.
    """
    params = PostgresLoader._provider_params({
        "source_id": "standort:771077015",
        "name": "Josephs-Hospital",
        "type": "pflegeheim_stationaer",
        "details": {"source": "standortverzeichnis"},
    })
    assert params["source_id"] == "standort:771077015"


def test_osm_records_without_a_source_id_still_get_one():
    """The scraper's records carry no source_id; that path must keep working."""
    params = PostgresLoader._provider_params({
        "name": "X",
        "type": "pflegeheim_stationaer",
        "details": {"osm_type": "node", "osm_id": 42, "source": "openstreetmap"},
    })
    assert params["source_id"] == "osm:node/42"


def test_a_record_with_neither_is_skipped():
    assert PostgresLoader._provider_params(
        {"name": "X", "type": "pflegeheim_stationaer", "details": {}}) is None


# ------------------------------------------------------------- schema guard


def test_schema_mismatch_is_refused(tmp_path):
    path = build_archive(tmp_path / "g.tar.gz", [sample_row()],
                         schema_migration="0001_init.sql")
    with pytest.raises(ValueError, match="cut against schema 0001_init.sql"):
        import_dataset("postgres://unused", path)


def test_schema_mismatch_can_be_overridden(tmp_path, monkeypatch):
    """The override must actually reach the loader, not just skip the check."""
    path = build_archive(tmp_path / "h.tar.gz", [sample_row()],
                         schema_migration="0001_init.sql")
    called = {}

    def fake_load(self, records):
        called["rows"] = len(list(records))
        from pipelines.load.postgres_loader import LoadReport
        return LoadReport(inserted=called["rows"])

    monkeypatch.setattr(PostgresLoader, "load_providers", fake_load)
    result = import_dataset("postgres://unused", path, allow_schema_mismatch=True)
    assert called["rows"] == 1
    assert result.manifest["schema_migration"] == "0001_init.sql"


def test_latest_migration_picks_the_newest(tmp_path):
    for name in ("0001_a.sql", "0010_b.sql", "0002_c.sql"):
        (tmp_path / name).touch()
    assert latest_migration(tmp_path) == "0010_b.sql"


def test_latest_migration_survives_an_empty_directory(tmp_path):
    assert latest_migration(tmp_path) == "unknown"


# ------------------------------------------------------------- integration


@integration
def test_export_refuses_to_write_an_empty_dataset(tmp_path):
    """An empty archive is worse than no archive: it looks like a valid release."""
    import psycopg

    with psycopg.connect(DSN) as conn:
        count = conn.execute(
            "SELECT count(*) FROM care_infrastructure WHERE type <> 'krankenkasse'"
        ).fetchone()[0]
    if count:
        pytest.skip("database holds providers; cannot test the empty case here")

    with pytest.raises(ValueError, match="refusing to write an empty dataset"):
        export_dataset(DSN, tmp_path / "empty.tar.gz")


@integration
def test_export_then_import_is_a_faithful_round_trip(tmp_path):
    result = export_dataset(DSN, tmp_path / "round.tar.gz")
    assert result.row_count > 0

    manifest, records = read_archive(result.path)
    assert manifest["row_count"] == result.row_count
    assert len(records) == result.row_count
    # Every provider carries coordinates; losing them would break the radius API.
    assert all(r["lat"] is not None and r["lon"] is not None for r in records)
    assert all(r["source_id"] for r in records)
    # The archive must carry its own licence, not just the release page.
    with tarfile.open(result.path) as archive:
        names = archive.getnames()
    assert {"providers.csv", "MANIFEST.json", "LICENSE.txt", "README.md"} <= set(names)
