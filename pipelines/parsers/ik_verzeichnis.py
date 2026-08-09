"""IK-Nummer directory from the official Kostenträgerdateien (story E1-S6).

The GKV insurer list carries no Institutionskennzeichen, so insurers are keyed
on their name — fragile across yearly publications and useless for matching
against other sources. The GKV-Spitzenverband publishes the missing mapping in
its *Kostenträgerdateien*: official, machine-readable, refreshed every quarter,
and not disallowed by robots.txt.

The files are EDIFACT-like; the segment we need states IK and name directly::

    IDK+100820488+99+Brandenburgische BKK'

Two things to know before touching this:

* **File names embed the quarter** (``BK06Q326.ke0``), so the current set has to
  be discovered from the page. A hardcoded URL would go stale silently.
* **The encoding is ISO-8859-1**, not UTF-8; decoding as UTF-8 mangles umlauts.

Scope: this resolves IKs for *Kostenträger* — the insurers. Care providers are
not in these files; their IKs live in the DCS data behind the insurer portals
(see the E1-S2 source decision).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

# --------------------------------------------------------------- primary source
#
# Schlüsselverzeichnis 8a of the Bewertungsausschuss (§ 87 SGB V): one
# authoritative row per insurer, giving the *Kassensitz-IK* — the identifier of
# the institution itself.
#
# This is preferred over the Kostenträgerdateien because an insurer holds
# several IKs: one seat IK but many billing IKs (AOK Nordost has three). Taking
# whichever the exchange files happened to list first stored an arbitrary one of
# them, so two consumers matching on `ik_nummer` could disagree.
BA_URL_TEMPLATE = (
    "https://institut-ba.de/service/schluesselverzeichnisse/S_8a_ABRIK_{version:03d}.PDF"
)

# The document is versioned, not dated, and the site 404s with an HTML page
# rather than a 404 status — so a real PDF has to be detected by its magic
# bytes. Probing starts here and walks upwards.
BA_KNOWN_VERSION = 35
BA_MAX_PROBE = 12

# "104127692 108018007 AOK Baden-Württemberg" -> Abrechnungs-IK, Kassensitz-IK, name
_BA_ROW = re.compile(r"^(\d{9})\s+(\d{9})\s+(.+)$", re.MULTILINE)

_BASE = "https://www.gkv-datenaustausch.de/leistungserbringer"

# Both sectors are needed, and neither is sufficient alone. The SGB XI (Pflege)
# files list an insurer's *Pflegekasse*; the SGB V files list the *Krankenkasse*
# — different institutions with different IKs. Measured on the real data: the
# Pflege set alone misses VIACTIV, SECURVITA, SBK and AOK Schwarzwald-Baar-
# Heuberg, while the SGB V set alone misses TK and hkk.
INDEX_URLS: tuple[str, ...] = (
    f"{_BASE}/sonstige_leistungserbringer/kostentraegerdateien_sle/kostentraegerdateien.jsp",
    f"{_BASE}/pflege/kostentraegerdateien_pflege/kostentraegerdateien.jsp",
)

# Kept for callers that want a single sector.
INDEX_URL = INDEX_URLS[0]

# href="/media/.../kostentraegerdateien_2/BK06Q326.ke0"
_FILE_LINK = re.compile(
    r'href="(/media/[^"]*kostentraegerdateien[^"]*\.(?:ke\d|KE\d))"', re.IGNORECASE
)

# IDK+<9-digit IK>+<type>+<name>'
_IDK_SEGMENT = re.compile(r"IDK\+(\d{9})\+(\d+)\+([^'\n]+)")

# Words that carry no identity: legal forms, filler, marketing suffixes.
# Dropping them lets "AOK - Die Gesundheitskasse für Niedersachsen" and
# "AOK Niedersachsen" reduce to the same tokens.
_NOISE_WORDS = {
    "krankenkasse", "gesundheitskasse", "die", "der", "das", "den", "des",
    "fuer", "in", "und", "am", "im", "ev", "gmbh", "ag", "kg", "koerperschaft",
    "ehem", "vormals",
}

# Entries in the directory that are NOT the health insurer we want. The care
# fund (Pflegekasse) and the eastern regional split carry their own, different
# Institutionskennzeichen — matching one of those would attach a plausible but
# wrong IK.
_NON_KRANKENKASSE = re.compile(r"pflegekasse|\bpk\b|\bost\b|/\s*ost", re.IGNORECASE)

# Tokens that identify a *category* of insurer, never an individual one.
# An entry reducing to these alone ("BKK S-H" loses its one-letter tokens and
# becomes just {"bkk"}) would be a subset of nearly every BKK and hand out a
# confidently wrong IK. Such entries are excluded from matching entirely.
# Note: "knappschaft" is deliberately NOT here — it names one specific insurer,
# not a category, and listing it made KNAPPSCHAFT unmatchable.
_GENERIC_TOKENS = {"bkk", "aok", "ikk", "lkk", "kk", "ek", "pflegekasse"}

# Short forms and abbreviations no rule reaches. Keys must be in the form
# `_normalise` produces — token sets, sorted and space-joined. Values are names
# as they appear in the directory.
#
# An explicit table beats loosening the matcher: a wrong IK is silent, a
# missing one shows up in the report.
ALIASES: dict[str, str] = {
    "techniker": "TK",
    "kaufmaennische": "KKH",
    "bkk siemens": "SBK",
    "handelskrankenkasse": "hkk",
    # The directory abbreviates Saarland to SL for this one.
    "aok pfalz rheinland saarland": "AOK Rheinland-Pfalz/SL",
    # PDF extraction glues the name into one token including the "und".
    "berlin brandenburg ikk": "IKKBrandenburgundBerlin",
}


def _tokens(name: str) -> frozenset[str]:
    """Reduce an insurer name to its identifying tokens.

    A set, not a string: the directory frequently reverses word order
    ("Merck BKK" vs "BKK Merck"), which a concatenated key cannot survive.
    Parenthetical annotations are dropped — "BKK mkk (ehem. BKK VBU)" is the
    same institution as "BKK mkk".
    """
    text = name.lower()
    for umlaut, replacement in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(umlaut, replacement)
    text = re.sub(r"\([^)]*\)", " ", text)              # drop annotations
    text = re.sub(r"betriebskrankenkasse", "bkk", text)
    text = re.sub(r"innungskrankenkasse", "ikk", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return frozenset(
        token for token in text.split()
        if token and token not in _NOISE_WORDS and len(token) > 1
    )


def _normalise(name: str) -> str:
    """Stable string form of the token set — used for alias lookups."""
    return " ".join(sorted(_tokens(name)))


def _concat(name: str) -> str:
    """Order-preserving, space-free key.

    PDF text extraction glues some words but not others
    ("BKKSchwarzwald‐Baar‐Heuberg"), so neither a token set nor a sorted
    concatenation lines up. Dropping the separators while keeping the original
    order does, and complements the token index, which handles the opposite
    problem of reversed word order.
    """
    text = name.lower()
    for umlaut, replacement in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(umlaut, replacement)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"betriebskrankenkasse", "bkk", text)
    text = re.sub(r"innungskrankenkasse", "ikk", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return "".join(w for w in text.split() if w not in _NOISE_WORDS)


def _is_too_generic(tokens: frozenset[str]) -> bool:
    """True when a name carries no distinguishing token of its own."""
    return not tokens or tokens <= _GENERIC_TOKENS


@dataclass
class MatchReport:
    """Which insurers got an IK and which did not — the latter must be visible."""

    matched: dict[str, str] = field(default_factory=dict)   # insurer name -> IK
    unmatched: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        total = len(self.matched) + len(self.unmatched)
        return len(self.matched) / total if total else 0.0

    def summary(self) -> str:
        total = len(self.matched) + len(self.unmatched)
        return f"matched={len(self.matched)}/{total} ({self.rate:.0%}) unmatched={len(self.unmatched)}"


class IKVerzeichnis:
    """IK ↔ insurer-name directory built from the Kostenträgerdateien."""

    def __init__(
        self,
        timeout: int = 30,
        user_agent: str = "CareGraphBot/0.1 (+https://github.com/LWSNLab/CareGraph)",
        overrides_path: Path | str | None = None,
    ) -> None:
        self.timeout = timeout
        self.headers = {"User-Agent": user_agent}
        self._by_tokens: dict[frozenset[str], str] = {}   # token set -> IK
        # PDF text extraction drops the spaces between words ("AOKNordost"), so
        # a set of tokens cannot line up with the same name from the PDF list.
        # The concatenated form survives that and is matched as a second key.
        self._by_concat: dict[str, str] = {}              # "aoknordost" -> IK
        self._raw: dict[str, str] = {}                    # original name -> IK
        self.overrides = self._load_overrides(overrides_path)

    @staticmethod
    def _load_overrides(overrides_path: Path | str | None) -> dict[str, str]:
        """Manually curated IK numbers for insurers absent from the files.

        Six insurers are in no Kostenträgerdatei at all (see the story). Rather
        than guess an IK — which would be silently wrong — the mapping stays
        empty until someone supplies an authoritative value.

        Mirrors the address-override mechanism, including being found
        regardless of the working directory.
        """
        if overrides_path is not None:
            candidates = [Path(overrides_path)]
        else:
            project_root = Path(__file__).resolve().parents[2]
            candidates = [
                Path("pipelines/data/ik_overrides.json"),
                project_root / "pipelines" / "data" / "ik_overrides.json",
            ]

        for candidate in candidates:
            if candidate.exists():
                with open(candidate, encoding="utf-8") as handle:
                    data = json.load(handle)
                # Keys starting with "_" are documentation, not insurers.
                entries = {
                    _normalise(name): ik
                    for name, ik in data.items()
                    if not name.startswith("_")
                }
                log.info("IK overrides: %d entries from %s", len(entries), candidate)
                return entries
        return {}

    # ------------------------------------------------------------- retrieval

    def discover_file_urls(self, index_url: str = INDEX_URL) -> list[str]:
        """Find the current quarter's files. Names rotate, so never hardcode."""
        response = requests.get(index_url, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        hrefs = dict.fromkeys(_FILE_LINK.findall(response.text))
        urls = [urljoin(index_url, href) for href in hrefs]
        log.info("discovered %d Kostenträgerdateien", len(urls))
        return urls

    def _fetch(self, url: str) -> str:
        response = requests.get(url, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        # ISO-8859-1 is what the files are; UTF-8 would corrupt the umlauts.
        return response.content.decode("iso-8859-1", errors="replace")

    # ------------------------------------------------- primary: Kassensitz-IK

    def _latest_ba_pdf(self) -> bytes | None:
        """Fetch the newest Schlüsselverzeichnis 8a.

        The version lives in the filename and the server answers unknown
        versions with an HTML page under a 200 status, so each candidate is
        verified by its PDF magic bytes rather than its status code.
        """
        newest: bytes | None = None
        for offset in range(BA_MAX_PROBE):
            version = BA_KNOWN_VERSION + offset
            try:
                response = requests.get(
                    BA_URL_TEMPLATE.format(version=version),
                    headers=self.headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as err:
                log.warning("Schlüsselverzeichnis v%03d: %s", version, err)
                break
            if not response.content.startswith(b"%PDF"):
                break          # first gap means we passed the newest version
            newest = response.content
            log.debug("Schlüsselverzeichnis v%03d available", version)
        return newest

    def load_kassensitz(self) -> int:
        """Index the authoritative Kassensitz-IK per insurer. Returns row count."""
        import io

        import pdfplumber

        content = self._latest_ba_pdf()
        if content is None:
            log.warning("Schlüsselverzeichnis 8a unavailable — falling back to exchange files only")
            return 0

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)

        rows = self.add_kassensitz_from_text(text)
        log.info("Kassensitz-IK directory: %d rows", rows)
        return rows

    def add_kassensitz_from_text(self, text: str) -> int:
        """Index the Kassensitz-IK rows of an already-extracted document.

        Split out from the download so the parsing can be tested offline.
        """
        rows = 0
        for _abrechnungs_ik, kassensitz_ik, name in _BA_ROW.findall(text):
            name = name.strip()
            tokens = _tokens(name)
            if not name or _is_too_generic(tokens):
                continue
            # setdefault: the exchange files are loaded afterwards and must not
            # overwrite the authoritative value.
            self._raw.setdefault(name, kassensitz_ik)
            self._by_tokens.setdefault(tokens, kassensitz_ik)
            self._by_concat.setdefault(_concat(name), kassensitz_ik)
            rows += 1
        return rows

    # ------------------------------------------------ fallback: exchange files

    def load(self, index_urls: tuple[str, ...] | str = INDEX_URLS) -> int:
        """Download and parse every file from every sector.

        Order matters: SGB V comes first so a Krankenkasse entry wins over the
        Pflegekasse of the same name — ``setdefault`` keeps the first seen.
        Returns the number of distinct IKs.
        """
        if isinstance(index_urls, str):
            index_urls = (index_urls,)

        for index_url in index_urls:
            try:
                urls = self.discover_file_urls(index_url)
            except requests.RequestException as err:
                log.warning("could not read index %s: %s", index_url, err)
                continue
            for url in urls:
                try:
                    self.add_from_text(self._fetch(url))
                except requests.RequestException as err:
                    # One unavailable file must not lose the rest of the directory.
                    log.warning("could not fetch %s: %s", url, err)

        log.info("IK directory: %d entries", len(self._raw))
        return len(self._raw)

    def load_all(self) -> int:
        """Build the directory from both sources, authoritative one first.

        Order is the whole point: the Kassensitz-IK is indexed before the
        exchange files, so `setdefault` keeps it and the billing IKs only fill
        gaps for insurers the Schlüsselverzeichnis does not list.
        """
        self.load_kassensitz()
        self.load()
        return len(self._raw)

    # --------------------------------------------------------------- parsing

    def add_from_text(self, text: str) -> int:
        """Parse IDK segments out of one file's content.

        Care-fund and regional-split entries are indexed separately: they are
        real institutions with their own IK, but they are not the health
        insurer CareGraph stores, so they must never win a match.
        """
        added = 0
        for match in _IDK_SEGMENT.finditer(text):
            ik, _type, name = match.group(1), match.group(2), match.group(3).strip()
            if not name:
                continue
            self._raw.setdefault(name, ik)
            tokens = _tokens(name)
            if not _NON_KRANKENKASSE.search(name) and not _is_too_generic(tokens):
                self._by_tokens.setdefault(tokens, ik)
                self._by_concat.setdefault(_concat(name), ik)
            added += 1
        return added

    def __len__(self) -> int:
        return len(self._raw)

    # -------------------------------------------------------------- matching

    def lookup(self, insurer_name: str) -> str | None:
        """Resolve one insurer name to its IK, or None.

        Three steps, each stricter than fuzzy string distance:

        1. identical token sets,
        2. the alias table for short forms that no rule reaches,
        3. one token set fully contained in the other — the most specific
           (largest) candidate wins, so "AOK Rheinland/Hamburg" is not matched
           by a mere "AOK".

        Nothing here guesses. A wrong IK would look perfectly plausible in the
        data and nobody would notice; a missing one shows up in the report.
        """
        wanted = _tokens(insurer_name)
        if not wanted:
            return None

        # A curated value outranks every heuristic below it.
        override = self.overrides.get(_normalise(insurer_name))
        if override:
            return override

        if wanted in self._by_tokens:
            return self._by_tokens[wanted]

        alias = ALIASES.get(_normalise(insurer_name))
        if alias:
            alias_tokens = _tokens(alias)
            if alias_tokens in self._by_tokens:
                return self._by_tokens[alias_tokens]

        concat = self._by_concat.get(_concat(insurer_name))
        if concat:
            return concat

        # The same guard as at index time, now on the query side: "R+V
        # Betriebskrankenkasse" loses its one-letter tokens and reduces to
        # {"bkk"}, which is contained in every BKK in the directory. Containment
        # would then return a confidently wrong IK.
        if _is_too_generic(wanted):
            return None

        contained = [
            tokens for tokens in self._by_tokens
            if tokens and (tokens <= wanted or wanted <= tokens)
        ]
        if contained:
            return self._by_tokens[max(contained, key=len)]
        return None

    def match_all(self, names: list[str]) -> MatchReport:
        """Resolve a batch, keeping the misses visible rather than dropping them."""
        report = MatchReport()
        for name in names:
            ik = self.lookup(name)
            if ik:
                report.matched[name] = ik
            else:
                report.unmatched.append(name)
        log.info("IK matching: %s", report.summary())
        return report
