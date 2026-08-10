"""Shared normalisation helpers used by both the exporter and the loader.

Kept in one place because the rules are subtle and would drift if copied:
"bundesweit" is a flag rather than 16 links, company insurers map to nothing,
and a longest-match is required so "Sachsen-Anhalt" is not read as "Sachsen".
"""

from __future__ import annotations

import re

# The 16 German federal states, canonical spelling.
BUNDESLAENDER: tuple[str, ...] = (
    "Baden-Württemberg",
    "Bayern",
    "Berlin",
    "Brandenburg",
    "Bremen",
    "Hamburg",
    "Hessen",
    "Mecklenburg-Vorpommern",
    "Niedersachsen",
    "Nordrhein-Westfalen",
    "Rheinland-Pfalz",
    "Saarland",
    "Sachsen",
    "Sachsen-Anhalt",
    "Schleswig-Holstein",
    "Thüringen",
)

# Longest first, so "Sachsen-Anhalt" wins over "Sachsen".
_BY_LENGTH = sorted(BUNDESLAENDER, key=len, reverse=True)


def parse_bundeslaender(
    geoffnet_in, is_bundesweit: bool = False, expand_bundesweit: bool = False
) -> list[str]:
    """Turn the GKV 'geöffnet in' text into canonical federal-state names.

    - ``bundesweit`` yields all 16 only when ``expand_bundesweit`` is set;
      otherwise none, because the fact already lives in the is_bundesweit flag.
    - ``betriebsbezogen …`` (company insurers) yields none — not publicly open.
    - Trailing qualifiers survive: "Schleswig-Holstein branchenbezogen" still
      maps to Schleswig-Holstein.
    """
    if expand_bundesweit and bool(is_bundesweit):
        return list(BUNDESLAENDER)

    result: list[str] = []
    for token in str(geoffnet_in).split(","):
        token = token.strip()
        match = next((state for state in _BY_LENGTH if token.startswith(state)), None)
        if match and match not in result:
            result.append(match)
    return result


# --------------------------------------------------------------------- website

# Hosts whose HTTPS port does not answer at all, so a link must use http.
# Measured 2026-08-10 by opening a TLS connection to every scheme-less host in
# the dataset (93 of them); these two time out.
#
# Deliberately NOT listed here: hosts that serve HTTPS with an invalid or
# mismatched certificate (bkk-deutsche-bank.de, bkk-miele.de,
# pflegeheim-michelberg.casa-reha.de). A broken certificate is the site's
# problem to fix; recommending http instead would be the wrong answer and would
# quietly downgrade a user's connection.
HTTP_ONLY_HOSTS = frozenset({
    "seniorenheim-eggmuehl.brk.de",
    "suedzucker-bkk.de",
})

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
# A host we are willing to publish: labels of alphanumerics/hyphens, at least
# one dot, and a plausible TLD. Shape only — it says nothing about reachability.
_HOSTLIKE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$", re.IGNORECASE)


def normalize_website(value: str | None) -> str | None:
    """Turn a stored website value into an absolute URL, or None.

    The GKV parser strips the scheme on purpose — the address scraper wants a
    bare host — but that form must not reach the API, where `website` is a link
    a client is expected to follow. Normalising here, at load time, keeps the
    database as the clean copy and leaves the parser's output alone.

    Already-absolute values are returned untouched: OSM supplies proper URLs and
    rewriting them would only invent differences. Anything whose host is not
    even shaped like a hostname yields None rather than a URL that cannot work.
    """
    if value is None:
        return None

    candidate = str(value).strip()
    if not candidate:
        return None

    if _SCHEME.match(candidate):
        return candidate

    # Protocol-relative ("//example.de/x") — the scheme is simply missing.
    candidate = candidate.lstrip("/")
    if not candidate:
        return None

    host = candidate.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].lower()
    if not _HOSTLIKE.match(host):
        return None

    bare = host.removeprefix("www.")
    scheme = "http" if host in HTTP_ONLY_HOSTS or bare in HTTP_ONLY_HOSTS else "https"
    return f"{scheme}://{candidate}"
