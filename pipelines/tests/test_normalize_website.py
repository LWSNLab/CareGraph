"""Tests for website normalisation (pipelines/common/normalize.py).

The GKV parser strips schemes deliberately, so the loader has to put one back
before the value reaches the API as a link.
"""

from __future__ import annotations

import pytest

from pipelines.common import HTTP_ONLY_HOSTS, normalize_website


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Scheme-less — the GKV parser's output shape.
        ("hek.de", "https://hek.de"),
        ("www.tk.de", "https://www.tk.de"),
        ("aok.de/bayern", "https://aok.de/bayern"),
        ("aok.de/baden-wuerttemberg/index.php", "https://aok.de/baden-wuerttemberg/index.php"),
        # Already absolute — OSM's shape. Returned untouched: rewriting a good
        # URL only invents differences between sources.
        ("https://pflegedienst-muster.de", "https://pflegedienst-muster.de"),
        ("http://www.example.de/x", "http://www.example.de/x"),
        ("HTTPS://Example.de/Path", "HTTPS://Example.de/Path"),
        # Protocol-relative: the scheme is simply missing.
        ("//example.de/x", "https://example.de/x"),
        # Surrounding whitespace survives neither form.
        ("  hek.de  ", "https://hek.de"),
    ],
)
def test_normalises_to_an_absolute_url(raw, expected):
    assert normalize_website(raw) == expected


@pytest.mark.parametrize("host", sorted(HTTP_ONLY_HOSTS))
def test_measured_http_only_hosts_get_http(host):
    # These two have no listening HTTPS port (measured 2026-08-10), so https
    # would produce a link that cannot be opened.
    assert normalize_website(host) == f"http://{host}"
    assert normalize_website(f"www.{host}") == f"http://www.{host}"
    assert normalize_website(f"{host}/impressum") == f"http://{host}/impressum"


def test_bad_certificate_hosts_stay_on_https():
    # A mismatched certificate is the site's problem. Downgrading to http would
    # weaken the connection we recommend, and these hosts do answer on 443.
    for host in ("bkk-deutsche-bank.de", "bkk-miele.de"):
        assert normalize_website(host) == f"https://{host}"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "///",
        "not a host",           # whitespace
        "localhost",            # no dot
        "hek",                  # no TLD
        "hek.d",                # TLD too short
        "-hek.de",              # label may not start with a hyphen
        "hek..de",              # empty label
        "1234",
    ],
)
def test_unusable_values_yield_none(raw):
    # Better no link than a URL that cannot work: `website` is published as
    # something a client is expected to follow.
    assert normalize_website(raw) is None


def test_shape_check_is_not_a_reachability_check():
    # A host can be well-formed and still be wrong. `skd-bkk.dewww.svlfg.de` is
    # two insurers' domains glued together by the PDF parser: it passes the shape
    # test, does not resolve, and is a parser bug rather than a normalisation
    # one. Documented so nobody mistakes this function for validation.
    assert normalize_website("skd-bkk.dewww.svlfg.de") == "https://skd-bkk.dewww.svlfg.de"


def test_idempotent():
    # The loader is re-run on a schedule; a second pass must not stack schemes.
    once = normalize_website("hek.de")
    assert normalize_website(once) == once
