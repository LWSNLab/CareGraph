"""Resolving the Postgres DSN, and refusing to send it over the network in the clear.

Two rules, both of which exist because the failure they prevent is silent.

**No default DSN.** A connection string with a username and password baked into
the source is one that eventually runs somewhere it should not, and a typo in an
environment variable then falls back to it instead of failing. The development
values live in `.env.example`, where they are visibly examples.

**No plaintext to a remote host.** `sslmode=disable` is right for a loopback
connection and for a container network on one host, and wrong for anything that
crosses a network — but nothing complains, the query simply travels readable.
The libpq default is worse than `disable`: an unset `sslmode` means `prefer`,
which tries TLS and then *silently* falls back to plaintext, so the connection
that looks encrypted in staging may not be in production.

The same rule and the same override name apply to the Go gateway; see
`internal/infrastructure/config.go`.
"""

from __future__ import annotations

import os

from psycopg.conninfo import conninfo_to_dict

# Environment variables the runners read, in order of precedence.
# INGEST_DATABASE_URL first: DATABASE_URL belongs to the read-only API role, and
# loading with it would fail on the first write.
DSN_ENV_VARS = ("INGEST_DATABASE_URL", "DATABASE_URL")

# Set this to run a pipeline against a remote host without TLS. Named for what it
# does rather than for an environment, so it cannot be mistaken for a general
# "this is development" switch — see the E4-S1 story on why there is no
# CAREGRAPH_ENV.
ALLOW_INSECURE_ENV = "CAREGRAPH_ALLOW_INSECURE_DB"

# The sslmode values that actually guarantee encryption. An allowlist, not a
# list of bad ones: anything unrecognised — a typo, a mode added by a future
# libpq, or no sslmode at all — then fails closed instead of being waved through.
#
# The first draft here was a denylist of {disable, allow, prefer} and it had
# exactly that hole: a DSN with no sslmode is the libpq default `prefer`, which
# falls back to plaintext silently, and it passed the guard because "" was not
# in the list of bad values.
SECURE_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})

# Hosts that never leave the machine. Everything else is treated as remote,
# including a Docker service name — from inside the process there is no way to
# tell a private bridge network from the open internet.
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

    This is what the runners call after parsing arguments, so that a bad
    configuration fails before any connection is attempted rather than midway
    through a load.
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

    psycopg understands both a URL and a libpq keyword string, so this uses its
    parser rather than a second one of our own. It reports what the DSN says and
    does not apply libpq's defaults, so an absent sslmode comes back as the empty
    string — which is not in SECURE_SSLMODES and therefore fails closed.
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

    An empty host means the Unix socket, and an absolute path means a socket
    directory; neither touches a network.
    """
    if not host:
        return True
    if host.startswith("/"):
        return True
    return host.strip().lower() in LOCAL_HOSTS
