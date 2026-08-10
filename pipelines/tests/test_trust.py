"""Tests for OS trust-store verification (pipelines/common/trust.py).

Context: requests validates against certifi's public-root bundle, which cannot
know about a root that a TLS-inspecting proxy's operator installed in the OS
store. On this project that made two IK sources unreachable and dropped coverage
from 92/93 to 76/93, while `openssl s_client` reported the connection as fine.
"""

from __future__ import annotations

import builtins
import sys

import pytest

from pipelines.common import trust


@pytest.fixture(autouse=True)
def reset_state():
    """The module remembers that it injected; each test needs a clean slate."""
    original = trust._injected
    trust._injected = False
    yield
    trust._injected = original


@pytest.fixture
def populated_store(monkeypatch):
    """A trust store with CAs in it, stated explicitly.

    Reading the real one is order-dependent: once truststore is injected,
    `ssl.create_default_context()` no longer reports a certificate count.
    """
    monkeypatch.setattr(trust, "_os_store_ca_count", lambda: 149)


def test_injects_and_reports_success(monkeypatch, populated_store):
    calls = []
    fake = type(sys)("truststore")
    fake.inject_into_ssl = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "truststore", fake)

    assert trust.use_system_trust_store() is True
    assert calls == [1]


def test_is_idempotent(monkeypatch, populated_store):
    """It mutates process-global ssl state, so it must not stack."""
    calls = []
    fake = type(sys)("truststore")
    fake.inject_into_ssl = lambda: calls.append(1)
    monkeypatch.setitem(sys.modules, "truststore", fake)

    trust.use_system_trust_store()
    trust.use_system_trust_store()
    trust.use_system_trust_store()

    assert calls == [1], "inject_into_ssl was called more than once"


def test_missing_dependency_warns_instead_of_failing_silently(monkeypatch, caplog):
    """A silent fallback would resurface downstream as 'source unavailable'.

    That misdirection is what made the original diagnosis take a day: the visible
    symptom was an unreachable GKV server, the cause was local TLS interception.
    """
    real_import = builtins.__import__

    def no_truststore(name, *args, **kwargs):
        if name == "truststore":
            raise ImportError("No module named 'truststore'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "truststore", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_truststore)

    with caplog.at_level("WARNING"):
        assert trust.use_system_trust_store() is False

    assert "truststore is not installed" in caplog.text
    assert "proxy" in caplog.text, "the warning must name the failure mode"


def test_truststore_is_a_declared_dependency():
    """It is required, not optional: without it an intercepted host looks down."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads(Path("pipelines/pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(pyproject["project"]["dependencies"])
    assert "truststore" in deps


def test_no_new_literal_verify_false_is_introduced():
    """Guard against the tempting shortcut in *new* code.

    The trust store changes *which* roots are trusted, not *whether* the
    certificate is checked, so nothing here needs `verify=False`.

    Scope, stated plainly so this test is not mistaken for a stronger guarantee:
    it catches a literal `verify=False` only. `AddressScraper._fetch` relaxes
    verification through a *variable* (`attempts = [(url, True), (url, False)]`)
    and is therefore invisible to this check. That fallback predates the trust
    store, exists for the three insurer hosts whose certificates are genuinely
    invalid, and is a deliberate trade — an unverified Impressum could feed a
    wrong address into the dataset. Narrowing it to an explicit per-host
    allowlist is tracked separately.
    """
    import ast
    from pathlib import Path

    # Parsed rather than grepped: a text search hits third-party code in .venv
    # and the prose in trust.py that forbids exactly this.
    offenders = []
    for path in Path("pipelines").rglob("*.py"):
        if {"tests", ".venv", "__pycache__"} & set(path.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (kw.arg == "verify"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is False):
                    offenders.append(f"{path}:{node.lineno}")
    assert not offenders, f"certificate verification disabled at {offenders}"


def test_empty_os_store_keeps_certifi(monkeypatch, caplog):
    """An empty OS store must not be trusted over certifi.

    Handing verification to a store with no CAs would fail *every* TLS
    connection — worse than the default it replaces. Alpine-based images ship
    without `ca-certificates`, so this is a realistic deployment state rather
    than a theoretical one.
    """
    injected = []
    fake = type(sys)("truststore")
    fake.inject_into_ssl = lambda: injected.append(1)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    monkeypatch.setattr(trust, "_os_store_ca_count", lambda: 0)

    with caplog.at_level("WARNING"):
        assert trust.use_system_trust_store() is False

    assert injected == [], "verification was handed to an empty trust store"
    assert "no CA certificates" in caplog.text
    assert "ca-certificates" in caplog.text, "the warning must say how to fix it"


def test_populated_os_store_is_used(monkeypatch):
    injected = []
    fake = type(sys)("truststore")
    fake.inject_into_ssl = lambda: injected.append(1)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    monkeypatch.setattr(trust, "_os_store_ca_count", lambda: 150)

    assert trust.use_system_trust_store() is True
    assert injected == [1]

