# src/address_scraper.py
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import urllib3
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class AddressScraper:
    """Scrapes street, PLZ, and city from health insurance company impressum/contact pages."""

    def __init__(self, timeout: int = 10, delay: float = 0.3, overrides_path: Path | str | None = None):
        self.timeout = timeout
        self.delay = delay
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
        }
        self.plz_pattern = re.compile(
            r"\b(\d{5})\s+([A-ZÄÖÜ][A-Za-zäöüß\s.\-]+)"
        )
        # Straße: entweder ein zusammengesetztes Wort mit Straßen-Endung
        # (z. B. "Franklinstraße", "Höhnerweg") ODER "Name + eigenständiges
        # Grundwort" (z. B. "Rosenthaler Straße", "Prenzlauer Allee"). Der
        # vorangestellte Teil ist bewusst NICHT gierig über Leerzeichen, damit
        # nicht ganze Sätze vor der Hausnummer eingefangen werden.
        self.street_pattern = re.compile(
            r"([A-ZÄÖÜ][A-Za-zäöüß.\-]*(?:straße|strasse|str\.|weg|platz|allee|ufer|ring|damm|zeile|chaussee)"
            r"|[A-ZÄÖÜ][A-Za-zäöüß.\-]+\s(?:Straße|Strasse|Str\.|Allee|Weg|Platz|Ufer|Ring|Damm|Zeile|Chaussee))"
            r"\s+(\d+\s*[a-zA-Z]?)\b"
            r"|(Postfach\s+\d+(?:\s*\d+)?)",
            re.IGNORECASE,
        )
        # Verbindungswörter mehrteiliger Ortsnamen ("Frankfurt am Main",
        # "Geislingen an der Steige") – nur in Kleinschreibung als Teil des Orts.
        self.city_connectors = {
            "am", "an", "der", "den", "im", "ob", "auf", "bei", "vor", "zur", "a",
        }
        # Präfixe mehrteiliger Ortsnamen ("Bad Homburg", "Sankt Augustin")
        self.city_prefixes = {"bad", "sankt", "st."}

        # Hosts, deren https-Port hängt (nur http nutzbar) – zur Laufzeit gelernt
        self._https_dead: set[str] = set()

        # Local manual overrides for bot-blocked sites (z. B. Miele, BMW,
        # Deutsche Bank – Werks-BKKs, deren Impressum nicht scrapebar ist).
        self.overrides = self._load_overrides(overrides_path)

    def _load_overrides(self, overrides_path: Path | str | None) -> dict:
        """Loads manual address overrides, robust gegen das Arbeitsverzeichnis.

        Wird kein Pfad übergeben, wird die Datei sowohl relativ zum aktuellen
        Verzeichnis ALS AUCH relativ zum Projekt-Root (…/data/) gesucht – damit
        sie auch aus dem Notebook (cwd = notebooks/) gefunden wird."""
        if overrides_path is not None:
            candidates = [Path(overrides_path)]
        else:
            project_root = Path(__file__).resolve().parent.parent
            candidates = [
                Path("data/manual_overrides.json"),          # cwd-relativ
                project_root / "data" / "manual_overrides.json",  # Projekt-Root
            ]

        for candidate in candidates:
            if candidate.exists():
                with open(candidate, encoding="utf-8") as f:
                    return json.load(f)

        print(
            "⚠️  Keine manual_overrides.json gefunden "
            f"(gesucht: {', '.join(str(c) for c in candidates)}) – "
            "bot-geblockte Kassen bleiben ggf. unaufgelöst."
        )
        return {}

    def _sanitize_domain(self, domain: str) -> str:
        if not domain or pd.isna(domain):
            return ""

        d = str(domain).strip().lower()

        # Fix Doppel-URLs im PDF (z. B. skd-bkk.dewww.svlfg.de)
        if "www." in d and not d.startswith("www."):
            d = d.split("www.")[0]

        match = re.search(r"^(.*?\.de|.*?\.com)(/.*)?$", d)
        if match:
            host_part = match.group(1)
            path_part = match.group(2) or ""
            try:
                host_part = host_part.encode("idna").decode("utf-8")
            except Exception:
                pass
            return f"{host_part}{path_part}"

        return d

    def _clean_city_name(self, raw_ort: str) -> str:
        """Isolates the city name and drops the noise that follows it on the page.

        Der Ort ist das erste Wort nach der PLZ; danach folgt auf Impressum-
        Seiten oft Text ("Kostenlose Servicenummer", "Vertreten durch …").
        Mehrteilige Orte über Verbindungswörter ("am Main", "an der Steige")
        bleiben erhalten."""
        raw_ort = re.split(
            r"\b(Telefon|Tel|Fon|Fax|Zentrale|Postanschrift|Postfach|Ihr|Kontakt|Impressum|E-Mail|Mail)\b",
            raw_ort,
            flags=re.IGNORECASE,
        )[0]
        tokens = raw_ort.split()
        if not tokens:
            return ""

        city = [tokens[0]]
        i = 1
        # Präfix-Städte: "Bad" + folgendes groß geschriebenes Wort ("Bad Homburg").
        if (
            tokens[0].lower() in self.city_prefixes
            and i < len(tokens)
            and tokens[i][:1].isupper()
        ):
            city.append(tokens[i])
            i += 1
        while (
            i < len(tokens)
            and tokens[i].islower()
            and tokens[i].lower() in self.city_connectors
        ):
            city.append(tokens[i])
            i += 1
            # Nach dem Verbindungswort das folgende groß geschriebene Wort mitnehmen.
            if i < len(tokens) and tokens[i][:1].isupper():
                city.append(tokens[i])
                i += 1

        return " ".join(city).strip(" ,.-")[:35]

    def _build_candidate_urls(self, domain: str) -> list[str]:
        sanitized = self._sanitize_domain(domain)
        if not sanitized:
            return []

        full_url = (
            f"https://{sanitized}"
            if not sanitized.startswith("http")
            else sanitized
        )
        parsed = urlparse(full_url)
        host = parsed.netloc or parsed.path.split("/")[0]

        # Spezialbehandlung für AOK: Zentrale Impressum-Seite
        if "aok.de" in host:
            return [
                "https://www.aok.de/pk/rechtliches/impressum/",
                "https://www.aok.de/pk/magazin/impressum/",
                full_url,
            ]

        # www.-Form bevorzugen (Redirects erledigen den Rest). Wenige, gängige
        # Pfade genügen – exotische Impressum-Pfade fängt anschließend die
        # Link-Verfolgung (_discover_impressum_urls) ab, statt hier alles zu raten.
        www_host = host if host.startswith("www.") else f"www.{host}"
        base = f"https://{www_host}"
        candidates = [
            f"{base}/impressum",
            f"{base}/kontakt",
            base,
        ]

        seen: set[str] = set()
        return [u for u in candidates if not (u in seen or seen.add(u))]

    # Überschrift, unter der das zentrale AOK-Impressum jede Region auflistet.
    AOK_REGION_HEADING = "Impressum für die regionalen Inhalte der AOK"

    def _extract_address_for_aok(
        self, html_text: str, kassen_name: str
    ) -> dict[str, str]:
        """Findet im zentralen AOK-Impressum die Adresse der passenden Region.

        Die Seite aok.de/pk/rechtliches/impressum/ listet ALLE regionalen AOKs
        unter je einer <h2> 'Impressum für die regionalen Inhalte der AOK <Region>'.
        Jede Sektion enthält neben der AOK-Adresse auch Aufsichtsbehörde und
        Werbeagenturen – deshalb grenzen wir strikt auf die Sektion der richtigen
        Region ab und nehmen dort die ERSTE Adresse (die der AOK selbst)."""
        soup = BeautifulSoup(html_text, "html.parser")
        body = soup.body or soup
        full = body.get_text(" ", strip=True)

        # Regionen aus den Überschriften einsammeln.
        regions = []
        for h in soup.find_all(["h1", "h2", "h3"]):
            text = h.get_text(" ", strip=True)
            if text.startswith(self.AOK_REGION_HEADING):
                label = text[len(self.AOK_REGION_HEADING):].strip()
                if label and label not in regions:
                    regions.append(label)
        if not regions:
            return {}

        # Kassenname der richtigen Region zuordnen (längster Treffer gewinnt,
        # damit "Sachsen-Anhalt" nicht mit "Sachsen" von AOK PLUS kollidiert).
        def norm(s: str) -> str:
            return re.sub(r"\s+", "", s.lower())

        name_norm = norm(kassen_name)
        matched = next(
            (r for r in sorted(regions, key=len, reverse=True) if norm(r) in name_norm),
            None,
        )
        if not matched:
            return {}

        # Sektion = Text von dieser Überschrift bis zur nächsten Regionsüberschrift.
        starts = sorted(
            i for i in (full.find(f"{self.AOK_REGION_HEADING} {r}") for r in regions)
            if i >= 0
        )
        start = full.find(f"{self.AOK_REGION_HEADING} {matched}")
        later = [i for i in starts if i > start]
        section = full[start: min(later) if later else len(full)]

        # Erste Adresse der Sektion: Straße, danach die folgende PLZ + Ort.
        street_match = self.street_pattern.search(section)
        if street_match:
            if street_match.group(1):
                strasse = f"{street_match.group(1).strip()} {street_match.group(2).strip()}"
            else:
                strasse = street_match.group(3).strip()  # Postfach
            plz_from = street_match.end()
        else:
            strasse, plz_from = "", 0

        plz_match = self.plz_pattern.search(section, plz_from)
        if not plz_match:
            return {}

        return {
            "strasse": strasse,
            "plz": plz_match.group(1),
            "ort": self._clean_city_name(plz_match.group(2)),
            "status": "Success",
        }

    def _fetch(self, url: str) -> "requests.Response | None":
        """GETs a URL, tolerating broken TLS and https-only-timeouts.

        Reihenfolge der Versuche: https mit + ohne Zertifikatsprüfung, danach
        http (manche Kassen-Server – z. B. suedzucker-bkk.de – antworten NUR
        über http und laufen bei https in einen ReadTimeout). Hosts, deren
        https-Port hängt, werden gemerkt, damit die restlichen Pfade nicht
        erneut in den Timeout laufen."""
        host = urlparse(url).netloc
        if url.startswith("https://") and host in self._https_dead:
            url = "http://" + url[len("https://"):]

        attempts = [(url, True), (url, False)]
        if url.startswith("https://"):
            attempts.append(("http://" + url[len("https://"):], False))

        for target, verify_ssl in attempts:
            try:
                time.sleep(self.delay)
                res = requests.get(
                    target,
                    headers=self.headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                    verify=verify_ssl,
                )
                if res.status_code == 200:
                    res.encoding = res.apparent_encoding or "utf-8"
                    return res
            except requests.exceptions.SSLError:
                continue
            except requests.exceptions.Timeout:
                if target.startswith("https://"):
                    self._https_dead.add(host)  # https hängt -> künftig http
                continue
            except Exception:
                # Broad on purpose: this loop walks several URL variants and any
                # of them may fail in an unforeseen way without that being an
                # error — a later attempt may still succeed. Logged with the
                # traceback at DEBUG so `--verbose` can explain a host that
                # never resolves, instead of failing silently.
                log.debug("fetch attempt failed: %s", target, exc_info=True)
                continue
        return None

    def _extract_from_text(self, text: str) -> dict[str, str] | None:
        """Findet PLZ + Ort und (optional) Straße in einem Textblock."""
        plz_match = self.plz_pattern.search(text)
        if not plz_match:
            return None
        plz = plz_match.group(1)
        ort = self._clean_city_name(plz_match.group(2))

        street_match = self.street_pattern.search(text)
        if street_match:
            if street_match.group(1):
                strasse = f"{street_match.group(1).strip()} {street_match.group(2).strip()}"
            else:
                strasse = street_match.group(3).strip()  # Postfach
        else:
            strasse = ""

        return {"strasse": strasse, "plz": plz, "ort": ort, "status": "Success"}

    def _discover_impressum_urls(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extracts the real Impressum/Kontakt links from a page's footer/nav.

        Deutsche Websites sind gesetzlich zu einem Impressum-Link verpflichtet.
        Diesem echten Link zu folgen ist deutlich zuverlässiger als feste Pfade
        zu raten (z. B. '?p=page&ID=1', 'impressum.html', '/de/impressum')."""
        found: list[str] = []
        for a in soup.find_all("a", href=True):
            label = (a.get_text() + " " + a["href"]).lower()
            if "impressum" in label or "kontakt" in label:
                url = urljoin(base_url, a["href"])
                if url not in found:
                    found.append(url)
        # Impressum vor Kontakt priorisieren
        found.sort(key=lambda u: 0 if "impressum" in u.lower() else 1)
        return found

    def scrape_address_for_domain(
        self, domain: str, kassen_name: str = ""
    ) -> dict[str, str]:
        sanitized_domain = self._sanitize_domain(domain)

        def bare_host(value: str) -> str:
            """Strip protocol and www. so overrides compare on the host alone."""
            for prefix in ("https://", "http://", "www."):
                value = value.replace(prefix, "")
            return value.strip("/")

        # 1. Robuster Check gegen manual_overrides
        clean_target = bare_host(sanitized_domain)
        for override_key, data in self.overrides.items():
            clean_override = bare_host(override_key)

            if clean_override in clean_target or clean_target in clean_override:
                return {
                    "strasse": data.get("strasse", ""),
                    "plz": data.get("plz", ""),
                    "ort": data.get("ort", ""),
                    "status": "Success (Override)",
                }

        candidate_urls = self._build_candidate_urls(domain)
        if not candidate_urls:
            return {"strasse": "", "plz": "", "ort": "", "status": "No Domain"}

        visited: set[str] = set()
        discovered: list[str] = []

        # 2. Geratene Standard-Pfade zuerst (schnell für die meisten Kassen).
        for url in candidate_urls:
            if url in visited:
                continue
            visited.add(url)
            res = self._fetch(url)
            if res is None:
                continue

            # Spezialfall AOK-Matching
            if "aok.de" in url and kassen_name:
                aok_match = self._extract_address_for_aok(res.text, kassen_name)
                if aok_match:
                    return aok_match

            soup = BeautifulSoup(res.text, "html.parser")
            text = (
                soup.body.get_text(separator=" ", strip=True)
                if soup.body
                else soup.get_text(separator=" ", strip=True)
            )
            result = self._extract_from_text(text)
            if result:
                return result

            # Echte Impressum-/Kontakt-Links für Phase 3 einsammeln.
            for link in self._discover_impressum_urls(soup, res.url):
                if link not in visited and link not in discovered:
                    discovered.append(link)

        # 3. Fallback: den auf der Seite gefundenen echten Impressum-Links folgen
        #    (auf die relevantesten begrenzen, Impressum-Links sind vorsortiert).
        for url in discovered[:4]:
            if url in visited:
                continue
            visited.add(url)
            res = self._fetch(url)
            if res is None:
                continue
            soup = BeautifulSoup(res.text, "html.parser")
            text = (
                soup.body.get_text(separator=" ", strip=True)
                if soup.body
                else soup.get_text(separator=" ", strip=True)
            )
            result = self._extract_from_text(text)
            if result:
                return result

        return {
            "strasse": "",
            "plz": "",
            "ort": "",
            "status": "Failed / Manual Check",
        }

    def enrich_dataframe(
        self, df: pd.DataFrame, website_column: str = "website"
    ) -> pd.DataFrame:
        enriched_df = df.copy()
        addresses = []

        total = len(enriched_df)
        print(f"🔍 Starte Adress-Scraping für {total} Krankenkassen...\n")

        for pos, (_, row) in enumerate(enriched_df.iterrows(), start=1):
            domain = row[website_column]
            name = row.get("name", domain)

            print(f"[{pos}/{total}] Scrape {name} ({domain})...")
            address_info = self.scrape_address_for_domain(
                domain, kassen_name=name
            )
            addresses.append(address_info)

        addr_df = pd.DataFrame(addresses)
        enriched_df["strasse"] = addr_df["strasse"].values
        enriched_df["plz"] = addr_df["plz"].values
        enriched_df["ort"] = addr_df["ort"].values
        enriched_df["scraping_status"] = addr_df["status"].values

        return enriched_df