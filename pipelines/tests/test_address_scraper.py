"""Unit tests for the insurer address scraper (story E1-S1).

All network access is faked; these tests cover the parsing and URL logic that
caused real defects in the past (regional AOK addresses, city-name noise,
wrapped URLs, override lookup, positional DataFrame assignment).
"""

from __future__ import annotations

import json

import pandas as pd
import pytest
import requests
from bs4 import BeautifulSoup

from pipelines.scrapers.address_scraper import AddressScraper


@pytest.fixture
def scraper() -> AddressScraper:
    # Point at a non-existent overrides file so tests are independent of the repo data.
    return AddressScraper(timeout=1, delay=0, overrides_path="/nonexistent/overrides.json")


# ------------------------------------------------------------ domain cleanup


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("barmer.de", "barmer.de"),
        ("BARMER.DE", "barmer.de"),
        ("  tk.de  ", "tk.de"),
        ("aok.de/nordost", "aok.de/nordost"),
        ("aok.de/baden-wuerttemberg/index.php", "aok.de/baden-wuerttemberg/index.php"),
    ],
)
def test_sanitize_domain_keeps_host_and_path(scraper, raw, expected):
    assert scraper._sanitize_domain(raw) == expected


def test_sanitize_domain_splits_concatenated_urls(scraper):
    """The PDF sometimes glues two URLs together (skd-bkk.dewww.svlfg.de)."""
    assert scraper._sanitize_domain("skd-bkk.dewww.svlfg.de") == "skd-bkk.de"


def test_sanitize_domain_handles_empty_and_na(scraper):
    assert scraper._sanitize_domain("") == ""
    assert scraper._sanitize_domain(None) == ""
    assert scraper._sanitize_domain(float("nan")) == ""


# --------------------------------------------------------------- city names


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Bremen", "Bremen"),
        ("Bochum Kostenlose Servicenummer", "Bochum"),
        ("Dresden Die IKK classic ist eine Kö", "Dresden"),
        ("Hamburg Telefon 040 123", "Hamburg"),
        ("Potsdam Umsatzsteuer-ID DE", "Potsdam"),
    ],
)
def test_clean_city_name_strips_trailing_noise(scraper, raw, expected):
    assert scraper._clean_city_name(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Frankfurt am Main", "Frankfurt am Main"),
        ("Geislingen an der Steige Vertreten", "Geislingen an der Steige"),
        ("Bad Homburg", "Bad Homburg"),
        ("Sankt Augustin", "Sankt Augustin"),
    ],
)
def test_clean_city_name_keeps_multi_word_city_names(scraper, raw, expected):
    """Regression: multi-part names used to be truncated to the first token."""
    assert scraper._clean_city_name(raw) == expected


def test_clean_city_name_on_empty_input(scraper):
    assert scraper._clean_city_name("") == ""


# ------------------------------------------------------------ candidate URLs


def test_candidate_urls_use_the_bare_host_for_subpages(scraper):
    """Regression: /impressum was appended to the full path (…/index.php/impressum)."""
    urls = scraper._build_candidate_urls("aok.de/baden-wuerttemberg/index.php")
    assert all("index.php/impressum" not in u for u in urls)


def test_candidate_urls_prefer_www_and_are_deduplicated(scraper):
    urls = scraper._build_candidate_urls("barmer.de")
    assert urls == [
        "https://www.barmer.de/impressum",
        "https://www.barmer.de/kontakt",
        "https://www.barmer.de",
    ]
    assert len(urls) == len(set(urls))


def test_candidate_urls_for_aok_point_at_the_central_impressum(scraper):
    urls = scraper._build_candidate_urls("aok.de/bayern")
    assert urls[0] == "https://www.aok.de/pk/rechtliches/impressum/"


def test_candidate_urls_empty_for_blank_domain(scraper):
    assert scraper._build_candidate_urls("") == []


# ------------------------------------------------------------ text extraction


def test_extracts_street_plz_and_city(scraper):
    result = scraper._extract_from_text("Herausgeber Barmer Axel-Springer-Straße 44 10969 Berlin")
    assert result == {
        "strasse": "Axel-Springer-Straße 44",
        "plz": "10969",
        "ort": "Berlin",
        "status": "Success",
    }


def test_street_regex_does_not_swallow_the_preceding_sentence(scraper):
    """Regression: a greedy pattern captured whole sentences before the number."""
    text = (
        "Arbeitsgemeinschaft von Körperschaften des öffentlichen Rechts "
        "Rosenthaler Straße 31 10178 Berlin"
    )
    assert scraper._extract_from_text(text)["strasse"] == "Rosenthaler Straße 31"


@pytest.mark.parametrize(
    "text, street",
    [
        ("Franklinstraße 50 60486 Frankfurt", "Franklinstraße 50"),
        ("Höhnerweg 2 69469 Weinheim", "Höhnerweg 2"),
        ("Prenzlauer Allee 96 10409 Berlin", "Prenzlauer Allee 96"),
        ("Tannenstraße 4 b 01099 Dresden", "Tannenstraße 4 b"),
        ("Marienstr. 122 32425 Minden", "Marienstr. 122"),
    ],
)
def test_recognises_street_spellings(scraper, text, street):
    assert scraper._extract_from_text(text)["strasse"] == street


def test_postfach_is_accepted_as_street(scraper):
    result = scraper._extract_from_text("Postfach 1234 20097 Hamburg")
    assert result["strasse"].startswith("Postfach")


def test_returns_none_without_a_postcode(scraper):
    assert scraper._extract_from_text("Nur Fließtext ohne Adresse") is None


def test_address_without_street_still_yields_plz_and_city(scraper):
    result = scraper._extract_from_text("Sitz der Kasse 66098 Saarbrücken")
    assert result["plz"] == "66098" and result["strasse"] == ""


# ------------------------------------------------------------- link discovery


def test_discovers_impressum_links_and_prioritises_them(scraper):
    html = """
    <html><body>
      <a href="/kontakt/">Kontakt</a>
      <a href="/impressum.html">Impressum</a>
      <a href="/leistungen">Leistungen</a>
    </body></html>
    """
    links = scraper._discover_impressum_urls(BeautifulSoup(html, "html.parser"), "https://x.de/")

    assert links[0] == "https://x.de/impressum.html"
    assert "https://x.de/kontakt/" in links
    assert all("leistungen" not in link for link in links)


def test_discovers_links_by_href_even_without_matching_text(scraper):
    """Real case: the link text was an icon, the path carried the meaning."""
    html = '<html><body><a href="/de/impressum">Rechtliches</a></body></html>'
    links = scraper._discover_impressum_urls(BeautifulSoup(html, "html.parser"), "https://x.de/")
    assert links == ["https://x.de/de/impressum"]


# --------------------------------------------------------------- AOK regions

AOK_HTML = """
<html><body>
  <h2>Impressum für die regionalen Inhalte der AOK Bayern</h2>
  <p>AOK Bayern - Die Gesundheitskasse Zentrale Carl-Wery-Straße 28 81739 München</p>
  <p>Aufsicht: Staatsministerium Haidenauplatz 1 81667 München</p>
  <h2>Impressum für die regionalen Inhalte der AOK Bremen/Bremerhaven</h2>
  <p>AOK Bremen/Bremerhaven Bürgermeister-Smidt-Straße 95 28195 Bremen</p>
  <h2>Impressum für die regionalen Inhalte der AOK Sachsen-Anhalt</h2>
  <p>AOK Sachsen-Anhalt Lüneburger Straße 4 39106 Magdeburg</p>
</body></html>
"""


@pytest.mark.parametrize(
    "insurer, street, city",
    [
        ("AOK Bayern - Die Gesundheitskasse", "Carl-Wery-Straße 28", "München"),
        ("AOK Bremen / Bremerhaven", "Bürgermeister-Smidt-Straße 95", "Bremen"),
        ("AOK Sachsen-Anhalt - Die Gesundheitskasse", "Lüneburger Straße 4", "Magdeburg"),
    ],
)
def test_aok_regional_address_is_picked_per_section(scraper, insurer, street, city):
    """Regression: every AOK used to resolve to the Berlin federal association."""
    result = scraper._extract_address_for_aok(AOK_HTML, insurer)
    assert result["strasse"] == street
    assert result["ort"] == city


def test_aok_section_does_not_leak_the_supervising_authority(scraper):
    """Each section also lists the regulator — the FIRST address must win."""
    result = scraper._extract_address_for_aok(AOK_HTML, "AOK Bayern")
    assert "Haidenauplatz" not in result["strasse"]


def test_aok_returns_empty_for_unknown_region(scraper):
    assert scraper._extract_address_for_aok(AOK_HTML, "AOK Irgendwo") == {}


def test_aok_returns_empty_when_page_has_no_region_headings(scraper):
    assert scraper._extract_address_for_aok("<html><body><p>x</p></body></html>", "AOK Bayern") == {}


# ----------------------------------------------------------------- overrides


def test_overrides_are_found_regardless_of_working_directory(tmp_path, monkeypatch):
    """Regression: running from notebooks/ silently loaded no overrides."""
    payload = {"bkk-x.de": {"strasse": "Werksweg 1", "plz": "12345", "ort": "Musterstadt"}}
    override_file = tmp_path / "manual_overrides.json"
    override_file.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.chdir(tmp_path)  # a directory that is not the project root
    scraper = AddressScraper(overrides_path=override_file)

    assert scraper.overrides == payload


def test_override_short_circuits_the_network(scraper, tmp_path):
    override_file = tmp_path / "o.json"
    override_file.write_text(
        json.dumps({"bkk-miele.de": {"strasse": "Carl-Miele-Str. 214", "plz": "33335", "ort": "Gütersloh"}}),
        encoding="utf-8",
    )
    s = AddressScraper(delay=0, overrides_path=override_file)

    result = s.scrape_address_for_domain("bkk-miele.de", "Betriebskrankenkasse Miele")

    assert result["status"] == "Success (Override)"
    assert result["ort"] == "Gütersloh"


def test_missing_overrides_file_warns_but_does_not_crash(capsys):
    s = AddressScraper(overrides_path="/definitely/not/here.json")
    assert s.overrides == {}
    assert "manual_overrides.json" in capsys.readouterr().out


# ---------------------------------------------------------------- no domain


def test_missing_domain_is_reported_not_raised(scraper):
    assert scraper.scrape_address_for_domain("")["status"] == "No Domain"


# ------------------------------------------------------------------- _fetch


def test_fetch_falls_back_to_http_when_https_times_out(monkeypatch, scraper):
    """Regression: suedzucker-bkk.de answers only over http."""
    attempted: list[str] = []

    def fake_get(url, **kwargs):
        attempted.append(url)
        if url.startswith("https://"):
            raise requests.exceptions.Timeout("hangs")
        response = requests.Response()
        response.status_code = 200
        response._content = b"<html>ok</html>"
        return response

    monkeypatch.setattr(requests, "get", fake_get)

    assert scraper._fetch("https://www.example.de/impressum") is not None
    assert attempted[-1].startswith("http://")


def test_fetch_remembers_hosts_whose_https_hangs(monkeypatch, scraper):
    """Avoid re-running into the same timeout for every candidate path."""
    def always_timeout(url, **kwargs):
        raise requests.exceptions.Timeout("hangs")

    monkeypatch.setattr(requests, "get", always_timeout)
    scraper._fetch("https://www.slow.de/impressum")

    assert "www.slow.de" in scraper._https_dead


def test_fetch_returns_none_when_everything_fails(monkeypatch, scraper):
    monkeypatch.setattr(requests, "get", lambda url, **kw: (_ for _ in ()).throw(requests.ConnectionError()))
    assert scraper._fetch("https://www.down.de") is None


# ------------------------------------------------------------ enrichment


def test_enrich_dataframe_assigns_positionally(monkeypatch):
    """Regression: label alignment shifted addresses when the index had gaps."""
    df = pd.DataFrame(
        {"name": ["A", "B"], "website": ["a.de", "b.de"]},
        index=[5, 9],  # deliberately non-contiguous
    )

    addresses = {
        "a.de": {"strasse": "Astr 1", "plz": "11111", "ort": "Astadt", "status": "Success"},
        "b.de": {"strasse": "Bstr 2", "plz": "22222", "ort": "Bstadt", "status": "Success"},
    }
    scraper = AddressScraper(delay=0, overrides_path="/nonexistent.json")
    monkeypatch.setattr(
        scraper, "scrape_address_for_domain", lambda domain, kassen_name="": addresses[domain]
    )

    enriched = scraper.enrich_dataframe(df)

    assert list(enriched["ort"]) == ["Astadt", "Bstadt"]
    assert list(enriched["plz"]) == ["11111", "22222"]
