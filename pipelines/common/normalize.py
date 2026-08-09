"""Shared normalisation helpers used by both the exporter and the loader.

Kept in one place because the rules are subtle and would drift if copied:
"bundesweit" is a flag rather than 16 links, company insurers map to nothing,
and a longest-match is required so "Sachsen-Anhalt" is not read as "Sachsen".
"""

from __future__ import annotations

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
