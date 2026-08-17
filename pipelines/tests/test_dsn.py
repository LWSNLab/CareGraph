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
    """No built-in development DSN to fall back to."""
    with pytest.raises(DSNError, match="no database DSN"):
        checked_dsn(missing)


def test_env_precedence_prefers_the_ingest_role(monkeypatch):
    """DATABASE_URL is the read-only role; loading with it fails on the first write."""
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
        # Unix socket.
        "postgres:///caregraph",
        # Socket directory.
        "host=/var/run/postgresql dbname=caregraph",
        # libpq keyword form.
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
        # sslmode omitted: libpq defaults to prefer and downgrades silently.
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
    """Setting sslmode outside the DSN is legitimate, not a false alarm."""
    monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)
    monkeypatch.setenv("PGSSLMODE", "require")
    assert checked_dsn("postgres://u:p@db.example.org:5432/caregraph")


# --- the override --------------------------------------------------------


def test_the_override_permits_remote_plaintext(monkeypatch):
    """For a container network on one host."""
    monkeypatch.setenv(ALLOW_INSECURE_ENV, "1")
    assert checked_dsn(REMOTE_PLAIN) == REMOTE_PLAIN


def test_an_empty_override_does_not_count(monkeypatch):
    """A present-but-empty variable is a common accident."""
    monkeypatch.setenv(ALLOW_INSECURE_ENV, "")
    with pytest.raises(DSNError):
        checked_dsn(REMOTE_PLAIN)


# --- unparseable input ---------------------------------------------------


def test_an_unparseable_dsn_is_refused_rather_than_waved_through(monkeypatch):
    """With no host to inspect, the safe assumption is remote."""
    monkeypatch.delenv(ALLOW_INSECURE_ENV, raising=False)
    with pytest.raises(DSNError):
        checked_dsn("host=db.example.org this is not = valid conninfo '")
