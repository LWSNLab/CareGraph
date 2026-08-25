"""Resolving the Postgres DSN, and refusing to send it over a network in the clear.

Two rules, both because the failure they prevent is silent. There is **no default
DSN**: a baked-in credential is one that eventually runs somewhere it should not,
and a typo in an environment variable would fall back to it instead of failing.
And **no plaintext to a remote host** — an unset `sslmode` is worse than
`disable`, because libpq treats it as `prefer` and falls back to plaintext without
saying so.

The Go gateway applies the same rule under the same override name; see
internal/infrastructure/dsn.go.
"""

from __future__ import annotations

import os

from psycopg.conninfo import conninfo_to_dict

# INGEST_DATABASE_URL first: DATABASE_URL is the read-only API role, and loading
# with it would fail on the first write.
DSN_ENV_VARS = ("INGEST_DATABASE_URL", "DATABASE_URL")

# Permits a plaintext connection to a non-local host. Named for what it does, so
# it cannot be mistaken for a general "this is development" switch.
ALLOW_INSECURE_ENV = "CAREGRAPH_ALLOW_INSECURE_DB"

# An allowlist, not a list of bad values: anything unrecognised — a typo, a future
# libpq mode, or no sslmode at all — then fails closed. A denylist of
# {disable, allow, prefer} let a DSN with no sslmode through, which is the
# dangerous case.
SECURE_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})

# Everything else counts as remote, including a Docker service name: from inside
# the process a private bridge network is indistinguishable from the internet.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class DSNError(RuntimeError):
    """The DSN is missing, or would connect insecurely."""


def dsn_from_env() -> str | None:
    """The DSN from the environment, or None when no variable is set."""
    for name in DSN_ENV_VARS:
        value = os.environ.get(name)
        if value and value.strip():
            return value
    return None


def checked_dsn(dsn: str | None) -> str:
    """Return a DSN that is present and safe to connect with, or raise DSNError.

    Called after argument parsing, so a bad configuration fails before anything
    connects rather than midway through a load.
    """
    if dsn is None or not dsn.strip():
        raise DSNError(
            "no database DSN — set $INGEST_DATABASE_URL or pass --dsn "
            "(copy .env.example to .env for the local development values)"
        )
    assert_transport_is_safe(dsn)
    return dsn


def assert_transport_is_safe(dsn: str) -> None:
    """Raise DSNError when dsn would reach a remote host without TLS."""
    if os.environ.get(ALLOW_INSECURE_ENV, "").strip():
        return

    host, sslmode = _host_and_sslmode(dsn)

    if _is_local(host):
        return
    if sslmode in SECURE_SSLMODES:
        return

    shown = sslmode or "unset, which libpq treats as 'prefer'"
    raise DSNError(
        f"refusing to connect to {host!r} with sslmode={shown}: the connection "
        f"would be readable on the network. Use sslmode=require (or verify-ca / "
        f"verify-full, which also check the certificate). If this host really is "
        f"local — a container network on a single machine, say — set "
        f"{ALLOW_INSECURE_ENV}=1."
    )


def _host_and_sslmode(dsn: str) -> tuple[str, str]:
    """Pull host and sslmode out of a DSN in either supported form.

    Uses psycopg's parser, which understands both a URL and a libpq keyword
    string, rather than a second one of our own. It does not apply libpq's
    defaults, so an absent sslmode comes back empty and fails closed.
    """
    try:
        parsed = conninfo_to_dict(dsn)
    except Exception as exc:  # psycopg raises its own ProgrammingError subclass
        # Unparseable is not a pass. If the host cannot be determined, the safe
        # assumption is that it is remote.
        raise DSNError(f"cannot parse the database DSN: {exc}") from exc

    host = str(parsed.get("host") or os.environ.get("PGHOST") or "")
    sslmode = str(parsed.get("sslmode") or os.environ.get("PGSSLMODE") or "").strip().lower()
    return host, sslmode


def _is_local(host: str) -> bool:
    """Whether a connection to host stays on this machine.

    An empty host or an absolute path is a Unix socket.
    """
    if not host:
        return True
    if host.startswith("/"):
        return True
    return host.strip().lower() in LOCAL_HOSTS
