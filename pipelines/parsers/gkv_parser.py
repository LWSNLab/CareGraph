# src/gkv_parser
from pathlib import Path
import re
import pandas as pd
import pdfplumber
import requests

class GKVParser:
    """Parses official GKV statutory health insurance lists (PDF or CSV)."""

    def __init__(self, file_path_or_url: str | Path, output_filename: str = "gkv_liste.pdf"):
        """
        :param file_path_or_url: Pfad zur lokalen Datei ODER Download-URL.
        :param output_filename: Dateiname für das Ziel-PDF im Ordner data/raw/ (z. B. 'gkv_liste_2026.pdf')
        """
        self.file_path_or_url = str(file_path_or_url)
        self.output_filename = output_filename
        self.raw_df = pd.DataFrame()
        self.cleaned_df = pd.DataFrame()

    def download_if_url(self, target_dir: Path = Path("data/raw")) -> Path:
        """Downloads the PDF if a URL was provided and saves it under output_filename."""
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / self.output_filename

        if self.file_path_or_url.startswith("http"):
            response = requests.get(self.file_path_or_url, timeout=15)
            response.raise_for_status()
            with open(target_path, "wb") as f:
                f.write(response.content)
            return target_path

        # Falls ein lokaler Pfad übergeben wurde
        return Path(self.file_path_or_url)

    # Spaltenüberschriften der GKV-Liste (dienen als Anker für die Spaltengrenzen)
    HEADER_LABELS = ("Krankenkassenname", "Homepage", "Zusatzbeitrag", "geöffnet")

    def parse_pdf(self) -> pd.DataFrame:
        """Extracts all insurer rows from the GKV PDF via word coordinates.

        The official PDF has no table grid lines, and single entries wrap across
        several visual lines (name, URL and region can each break). pdfplumber's
        ``extract_tables()`` therefore drops or merges rows. We instead assign
        every word to one of the four columns by its x-position and treat a line
        that carries a Zusatzbeitrag value ("x,xx %") as the start of a new
        entry; lines without such a value are wrapped continuations of the
        entry above and get appended to the matching column.
        """
        pdf_path = self.download_if_url()

        extracted_rows = []

        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                extracted_rows.extend(self._parse_page(page))

        if not extracted_rows:
            raise ValueError(f"Keine Tabellendaten in '{pdf_path}' gefunden.")

        self.raw_df = pd.DataFrame(extracted_rows, columns=[0, 1, 2, 3])
        return self._clean_data()

    def _parse_page(self, page) -> list[list[str]]:
        """Reconstructs the 4-column rows of a single PDF page from word boxes."""
        words = page.extract_words()
        if not words:
            return []

        # Linke Spaltenkanten aus der Kopfzeile ableiten (robust über Jahrgänge).
        # Fallback auf die bekannten x-Positionen, falls die Kopfzeile fehlt.
        col_edges = self._column_edges(words)

        def col_index(x0: float) -> int:
            idx = 0
            for i, edge in enumerate(col_edges):
                if x0 >= edge - 3:  # kleine Toleranz gegen Rundung
                    idx = i
            return idx

        # Wörter zu visuellen Zeilen gruppieren (per y-Position / 'top').
        lines: dict[int, list] = {}
        for w in words:
            lines.setdefault(round(w["top"] / 3), []).append(w)

        rows: list[list[str]] = []
        for key in sorted(lines):
            cells = ["", "", "", ""]
            for w in sorted(lines[key], key=lambda w: w["x0"]):
                ci = col_index(w["x0"])
                cells[ci] = (cells[ci] + " " + w["text"]).strip()

            joined = " ".join(cells).strip().lower()
            if not joined:
                continue
            # Kopf- und Seitenzeilen überspringen
            if "krankenkassenname" in joined or "krankenkassenliste" in joined:
                continue
            if re.match(r"^seite \d", joined):
                continue

            # Neuer Eintrag = Zeile mit Zusatzbeitrag-Wert; sonst Fortsetzung.
            if re.search(r"\d", cells[2]):
                rows.append(cells)
            elif rows:
                for i in range(4):
                    if cells[i]:
                        rows[-1][i] = (rows[-1][i] + " " + cells[i]).strip()
        return rows

    def _column_edges(self, words) -> list[float]:
        """Left x-edge of each of the 4 columns, derived from the header row."""
        edges: dict[str, float] = {}
        for w in words:
            if w["text"] in self.HEADER_LABELS and w["text"] not in edges:
                edges[w["text"]] = w["x0"]
        if len(edges) == len(self.HEADER_LABELS):
            return [edges[label] for label in self.HEADER_LABELS]
        # Fallback: bekannte Positionen der offiziellen Liste
        return [38.0, 165.1, 286.6, 373.6]

    def _clean_data(self) -> pd.DataFrame:
        """Normalizes and cleans extracted PDF/CSV data into standardized columns."""
        df = self.raw_df.copy()

        # Verbleibende Kopfzeilen entfernen (nur exakter Header-Titel, damit
        # echte Namen wie "VIACTIV Krankenkasse" nicht fälschlich rausfallen).
        df = df[~df[0].str.contains("Krankenkassenname", case=False, na=False)]

        # Map typical GKV columns (4-column structure): Name | Website | Zusatzbeitrag | Geöffnet in
        if len(df.columns) >= 4:
            df = df.iloc[:, :4]
            df.columns = ["name", "website", "zusatzbeitrag_raw", "geoffnet_in"]
        else:
            raise ValueError(f"Unerwartete Spaltenanzahl im Dokument: {len(df.columns)}")

        # Clean Website (remove http/https prefixes for clean domain scraping later)
        df["website"] = (
            df["website"]
            .str.lower()
            .str.replace(r"\s+", "", regex=True)  # Zeilenumbrüche in URLs entfernen
            .str.replace(r"^https?://", "", regex=True)
            .str.replace(r"^www\.", "", regex=True)
            .str.strip("/")
        )

        # Clean Zusatzbeitrag (% to Float: e.g. "2,98 %" -> 2.98)
        df["zusatzbeitrag"] = (
            df["zusatzbeitrag_raw"]
            .str.replace("%", "", regex=False)
            .str.replace(",", ".", regex=False)
            .str.extract(r"(\d+\.\d+|\d+)")
            .astype(float)
        )

        # Flag for nation-wide accessibility
        df["is_bundesweit"] = df["geoffnet_in"].str.contains("bundesweit", case=False, na=False)

        # Umbruch-Artefakte in zusammengesetzten Wörtern zusammenführen,
        # z. B. "Mecklenburg- Vorpommern" -> "Mecklenburg-Vorpommern".
        # (Nur "Wort- Wort"; der Namens-Trenner "AOK - Die" hat einen Space
        # VOR dem Bindestrich und bleibt dadurch unberührt.)
        wrap_hyphen = (r"(?<=\w)-\s+(?=\w)", "-")
        df["name"] = df["name"].str.replace(*wrap_hyphen, regex=True).str.strip()
        df["geoffnet_in"] = df["geoffnet_in"].str.replace(*wrap_hyphen, regex=True).str.strip()

        self.cleaned_df = df[["name", "website", "zusatzbeitrag", "geoffnet_in", "is_bundesweit"]].dropna(subset=["name"])
        return self.cleaned_df


# Quick Local Test Runner
if __name__ == "__main__":
    # Beispiel mit Versionierung im Dateinamen:
    year = 2026
    pdf_filename = f"gkv_liste_{year}.pdf"
    sample_pdf_path = Path("data/raw") / pdf_filename

    if sample_pdf_path.exists():
        parser = GKVParser(file_path_or_url=sample_pdf_path, output_filename=pdf_filename)
        df = parser.parse_pdf()
        print(f"Erfolgreich geparst aus '{pdf_filename}':")
        print(df.head())
    else:
        print(f"Datei '{sample_pdf_path}' nicht gefunden. Bitte ablegen oder URL übergeben.")