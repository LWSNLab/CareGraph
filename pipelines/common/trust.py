"""Verify TLS against the operating system's trust store.

`requests` validates against the CA bundle shipped in `certifi`, which contains
public roots only. That is the right default, but it fails wherever traffic is
re-signed by a TLS-inspecting proxy: the proxy's root is installed in the OS
trust store by whoever runs the network, and certifi cannot know about it.

Observed on this project (2026-08-10): requests to
`www.gkv-datenaustausch.de` — the source of the IK directory — came back with

    SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    unable to get local issuer certificate

The certificate presented was not the GKV's:

    CN=gkv-spitzenverband.de, O=Zscaler Inc.
    CN=Zscaler Intermediate Root CA (zscalerthree.net)

A Zscaler appliance was inspecting those domains — selectively, since pypi.org
was not intercepted. The Zscaler root is present in the macOS system keychain, so
`openssl s_client` reported `Verify return code: 0 (ok)` while `requests` failed.
Two IK sources became unreachable and coverage dropped from 92/93 to 76/93.

**This is not a workaround for a broken certificate, and it is not a bypass.**
Verification stays fully enabled; the only change is *which* set of roots is
trusted — the one the machine's administrator configured. On a machine without
interception (CI, production) the OS store validates the real certificate exactly
as certifi would, so this is safe to call unconditionally.

Never replace this with `verify=False`. These are requests to institutions in the
statutory health system; accepting any certificate would make the connection
unauthenticated for the sake of convenience.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_injected = False


def use_system_trust_store() -> bool:
    """Route TLS verification through the OS trust store. Returns True if active.

    Call once from an entry point, never at import time of a library module: it
    changes process-global `ssl` behaviour, which a module import should not do
    as a side effect.
    """
    global _injected
    if _injected:
        return True

    try:
        import truststore
    except ImportError:
        # Loud, not silent. Without this the failure resurfaces as an unrelated
        # "source unavailable" further downstream, which is exactly the kind of
        # misdirection that cost a day here.
        log.warning(
            "truststore is not installed — TLS will be verified against certifi only. "
            "Behind a TLS-inspecting proxy, sources will appear unreachable."
        )
        return False

    # Switching to the OS store means trusting whatever it holds — including
    # nothing. Some minimal container bases (Alpine most notably) ship without
    # `ca-certificates`, and an empty store would fail *every* TLS connection,
    # which is strictly worse than the certifi default this replaces. Verify the
    # store is populated before handing verification over to it.
    if _os_store_ca_count() == 0:
        log.warning(
            "the OS trust store holds no CA certificates — keeping certifi. "
            "In a container, install ca-certificates in the image."
        )
        return False

    truststore.inject_into_ssl()
    _injected = True
    log.debug("TLS verification now uses the OS trust store")
    return True


def _os_store_ca_count() -> int:
    """How many CA certificates the OS trust store offers, 0 if it cannot be read."""
    import ssl

    try:
        context = ssl.create_default_context()
        return context.cert_store_stats().get("x509_ca", 0)
    except Exception:  # pragma: no cover - a store this broken is not usable
        log.debug("could not read the OS trust store", exc_info=True)
        return 0
