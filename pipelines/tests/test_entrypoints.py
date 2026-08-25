"""Tests for the CLI entry points' failure behaviour.

A scheduled pipeline is only as good as its exit code: a run that fails and
returns 0 is worse than one that crashes, because nothing notices.
"""

from __future__ import annotations

import sys

import pytest

from pipelines import run_gkv


@pytest.fixture(autouse=True)
def no_global_tls_mutation(monkeypatch):
    """Keep `main()` from injecting truststore into the process-wide ssl module.

    `use_system_trust_store()` replaces `ssl.create_default_context`, which leaks
    into every test that runs afterwards — it made three unrelated trust tests
    fail depending on execution order. The entry point's TLS wiring is covered in
    test_trust.py; here it is noise.
    """
    monkeypatch.setattr(run_gkv, "use_system_trust_store", lambda: True)


def test_run_gkv_returns_nonzero_when_parsing_fails(monkeypatch, caplog):
    """Previously `main()` returned None, so the shell always saw success."""
    class Boom:
        def __init__(self, *a, **kw):
            pass

        def parse_pdf(self):
            raise ValueError("Keine Tabellendaten gefunden")

    monkeypatch.setattr(run_gkv, "GKVParser", Boom)
    monkeypatch.setattr(sys, "argv", ["run_gkv", "missing.pdf", "--no-scrape"])

    with caplog.at_level("ERROR"):
        code = run_gkv.main()

    assert code == 1
    # log.exception, so the traceback is part of the record — without it an
    # unexpected failure in a cron run is a one-line mystery.
    record = next(r for r in caplog.records if r.levelname == "ERROR")
    assert record.exc_info is not None, "failure logged without a traceback"
    assert "Keine Tabellendaten" in caplog.text


def test_run_gkv_returns_zero_on_success(monkeypatch, tmp_path):
    import pandas as pd

    frame = pd.DataFrame([{
        "name": "PyTest Kasse", "website": "kasse.de",
        "zusatzbeitrag": 2.0, "geoffnet_in": "Bayern", "is_bundesweit": False,
    }])

    class Fake:
        def __init__(self, *a, **kw):
            pass

        def parse_pdf(self):
            return frame

    monkeypatch.setattr(run_gkv, "GKVParser", Fake)
    monkeypatch.setattr(sys, "argv", [
        "run_gkv", "x.pdf", "--no-scrape", "--out", str(tmp_path),
    ])

    assert run_gkv.main() == 0
    assert (tmp_path / "krankenkassen_2026.csv").exists()

