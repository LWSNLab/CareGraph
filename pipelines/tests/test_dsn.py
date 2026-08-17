"""The DSN guard: what it lets through, what it stops, and what it must not miss."""

import pytest

from pipelines.common.dsn import (
    ALLOW_INSECURE_ENV,
    DSNError,
    checked_dsn,
    dsn_from_env,
)

LOCAL = "postgres://u:p@localhost:5433/caregraph?sslmode=disable"
REMOTE_PLAIN = "postgres://u:p@db.example.org:5432/caregraph?sslmode=disable"


# --- the DSN has to be supplied ------------------------------------------


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_a_missing_dsn_is_an_error_not_a_fallback(missing):
    """There is deliberately no built-in development DSN to fall back to."""
    with pytest.raises(DSNError, match="no database DSN"):
        checked_dsn(missing)


def test_env_precedence_prefers_the_ingest_role(monkeypatch):
    """DATABASE_URL is the read-only API role; loading with it fails on first write."""
    monkeypatch.setenv("INGEST_DATABASE_URL", "postgres://ingest@localhost/x")
    monkeypatch.setenv("DATABASE_URL", "postgres://readonly@localhost/x")
    assert "ingest" in dsn_from_env()


def test_env_falls_back_to_database_url(monkeypatch):
    monkeypatch.delenv("INGEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://readonly@localhost/x")
    assert "readonly" in dsn_from_env()


def test_no_env_is_none_rather_than_a_guess(monkeypatch):
    for name in ("INGEST_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert dsn_from_env() is None


# --- local connections stay allowed --------------------------------------


@pytest.mark.parametrize(
    "dsn",
    [
        LOCAL,
        "postgres://u:p@127.0.0.1:5433/caregraph?sslmode=disable",
        "postgres://u:p@[::1]:5433/caregraph?sslmode=disable",
        # No host: the Unix socket, which touches no network.
        "postgres:///caregraph",
        # An explicit socket directory.
        "host=/var/run/postgresql dbname=caregraph",
        # libpq keyword form, not just URLs.
        "host=localhost port=5433 dbname=caregraph sslmode=disable",
    ],
)
def test_plaintext_is_fine_when_it_does_not_leave_the_machine(dsn, monkeypatch):
    monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)
    assert checked_dsn(dsn) == dsn


# --- remote plaintext is refused -----------------------------------------


@pytest.mark.parametrize(
    "dsn",
    [
        REMOTE_PLAIN,
        "postgres://u:p@db.example.org:5432/caregraph?sslmode=allow",
        "postgres://u:p@db.example.org:5432/caregraph?sslmode=prefer",
        # sslmode omitted entirely — libpq defaults to prefer and falls back to
        # plaintext without a word, so this must be refused too.
        "postgres://u:p@db.example.org:5432/caregraph",
        "host=db.example.org dbname=caregraph",
        # A Docker service name is remote as far as this process can tell.
        "postgres://u:p@db:5432/caregraph?sslmode=disable",
    ],
)
def test_remote_plaintext_is_refused(dsn, monkeypatch):
    monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)
    with pytest.raises(DSNError, match="readable on the network"):
        checked_dsn(dsn)


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_remote_with_tls_is_allowed(mode, monkeypatch):
    monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)
    dsn = f"postgres://u:p@db.example.org:5432/caregraph?sslmode={mode}"
    assert checked_dsn(dsn) == dsn


def test_pgsslmode_env_counts_as_configuration(monkeypatch):
    """Setting sslmode outside the DSN is legitimate and must not be a false alarm."""
    monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)
    monkeypatch.setenv("PGSSLMODE", "require")
    assert checked_dsn("postgres://u:p@db.example.org:5432/caregraph")


# --- the override --------------------------------------------------------


def test_the_override_permits_remote_plaintext(monkeypatch):
    """For a container network on a single host, where the operator knows better."""
    monkeypatch.setenv(ALLOW_INSECURE_ENV, "1")
    assert checked_dsn(REMOTE_PLAIN) == REMOTE_PLAIN


def test_an_empty_override_does_not_count(monkeypatch):
    """An unset-but-present variable is a common accident; it must not disable the guard."""
    monkeypatch.setenv(ALLOW_INSECURE_ENV, "")
    with pytest.raises(DSNError):
        checked_dsn(REMOTE_PLAIN)


# --- unparseable input ---------------------------------------------------


def test_an_unparseable_dsn_is_refused_rather_than_waved_through(monkeypatch):
    """If the host cannot be determined, the safe assumption is that it is remote."""
    monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)
    with pytest.raises(DSNError):
        checked_dsn("host=db.example.org this is not = valid conninfo '")
