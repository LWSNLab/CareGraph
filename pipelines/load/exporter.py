import json
from pathlib import Path

import pandas as pd

from pipelines.common import BUNDESLAENDER as _BUNDESLAENDER
from pipelines.common import parse_bundeslaender as _parse_bundeslaender


class DataExporter:
    """Handles exporting enriched DataFrame to CSV, JSON, and SQL files."""

    def __init__(self, output_dir: Path | str = Path("data/processed")):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_csv(
        self, df: pd.DataFrame, filename: str = "krankenkassen.csv"
    ) -> Path:
        """Exports DataFrame to a clean CSV file formatted for Excel/UTF-8."""
        target_path = self.output_dir / filename
        df.to_csv(target_path, index=False, encoding="utf-8-sig")
        print(f"💾 CSV gespeichert unter: {target_path}")
        return target_path

    def export_json(
        self, df: pd.DataFrame, filename: str = "krankenkassen.json"
    ) -> Path:
        """Exports DataFrame to structured JSON format."""
        target_path = self.output_dir / filename
        records = df.to_dict(orient="records")
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"💾 JSON gespeichert unter: {target_path}")
        return target_path

    # Spalten, die eingefügt/aktualisiert werden (id & updated_at werden von der DB verwaltet).
    SQL_COLUMNS = [
        "name",
        "website",
        "zusatzbeitrag",
        "geoffnet_in",
        "is_bundesweit",
        "strasse",
        "plz",
        "ort",
        "scraping_status",
    ]

    @staticmethod
    def _sql_str(val) -> str:
        """String-Literal für Postgres (mit Escaping) oder NULL."""
        if val is None or pd.isna(val) or str(val).strip() == "":
            return "NULL"
        return "'" + str(val).strip().replace("'", "''") + "'"

    @staticmethod
    def _sql_num(val) -> str:
        """Numerisches Literal (2 Nachkommastellen, passend zu NUMERIC(4,2)) oder NULL."""
        return f"{float(val):.2f}" if pd.notna(val) else "NULL"

    @staticmethod
    def _sql_bool(val) -> str:
        if pd.isna(val):
            return "NULL"
        return "TRUE" if bool(val) else "FALSE"

    # Canonical master data — shared with the Postgres loader so the two cannot
    # drift apart (see pipelines/common/normalize.py).
    BUNDESLAENDER = list(_BUNDESLAENDER)

    def _parse_bundeslaender(
        self, geoffnet_in, is_bundesweit: bool, expand_bundesweit: bool
    ) -> list[str]:
        """Zerlegt das 'geöffnet in'-Feld in eine Liste kanonischer Bundesländer.

        Delegiert an die gemeinsame Normalisierung; siehe dort für die Regeln
        zu 'bundesweit', Werks-BKKs und mehrteiligen Ländernamen."""
        return _parse_bundeslaender(geoffnet_in, is_bundesweit, expand_bundesweit)

    def _rls_block(self, table_name: str) -> str:
        """Row-Level-Security mit öffentlicher Leseregel für eine Tabelle."""
        return (
            f"alter table {table_name} enable row level security;\n"
            f'drop policy if exists "Public read access" on {table_name};\n'
            f'create policy "Public read access"\n'
            f"    on {table_name} for select\n"
            f"    to anon, authenticated\n"
            f"    using (true);\n\n"
        )

    def export_sql(
        self,
        df: pd.DataFrame,
        filename: str = "krankenkassen_inserts.sql",
        table_name: str = "krankenkassen",
        enable_rls: bool = True,
        normalize_states: bool = True,
        expand_bundesweit: bool = False,
    ) -> Path:
        """Generates an idempotent PostgreSQL/Supabase ingest script.

        - Identity-Spalte statt SERIAL (moderne Postgres-Empfehlung).
        - UNIQUE(name) + ein einziges ``INSERT … ON CONFLICT … DO UPDATE`` (Upsert):
          mehrfaches Ausführen erzeugt KEINE Duplikate, sondern aktualisiert.
        - Optionaler Row-Level-Security-Block mit öffentlicher Leseregel
          (Supabase Best Practice für Tabellen im public-Schema).
        - ``normalize_states``: schreibt die Bundesländer in eine Stammtabelle
          ``bundeslaender`` und verknüpft sie über die n:m-Tabelle
          ``krankenkasse_bundesland`` (statt CSV-Text im Feld geoffnet_in).
        - ``expand_bundesweit``: wenn True, werden bundesweite Kassen mit ALLEN
          16 Ländern verknüpft; sonst tragen sie keine Links (Info bleibt im
          Flag is_bundesweit).
        """
        target_path = self.output_dir / filename
        cols = self.SQL_COLUMNS
        junction = "krankenkasse_bundesland"

        parts = [
            "-- Auto-generated GKV Data Ingest (PostgreSQL / Supabase)\n",
            "-- Idempotent: erneutes Ausführen aktualisiert bestehende Zeilen (Upsert auf name).\n\n",
            f"create table if not exists {table_name} (\n"
            "    id              bigint generated always as identity primary key,\n"
            "    name            text not null unique,\n"
            "    website         text,\n"
            "    zusatzbeitrag   numeric(4,2),\n"
            "    geoffnet_in     text,\n"
            "    is_bundesweit   boolean,\n"
            "    strasse         text,\n"
            "    plz             text,\n"
            "    ort             text,\n"
            "    scraping_status text,\n"
            "    updated_at      timestamptz not null default now()\n"
            ");\n\n",
        ]

        if normalize_states:
            parts.append(
                "create table if not exists bundeslaender (\n"
                "    id   smallint generated always as identity primary key,\n"
                "    name text not null unique\n"
                ");\n\n"
                f"create table if not exists {junction} (\n"
                f"    krankenkasse_id bigint   not null references {table_name}(id) on delete cascade,\n"
                "    bundesland_id   smallint not null references bundeslaender(id) on delete cascade,\n"
                "    primary key (krankenkasse_id, bundesland_id)\n"
                ");\n\n"
            )

        if enable_rls:
            parts.append(
                "-- Row Level Security: öffentliche, nur-lesende Referenzdaten.\n"
                "-- Schreibzugriffe laufen über service_role / SQL-Editor (umgeht RLS).\n"
            )
            parts.append(self._rls_block(table_name))
            if normalize_states:
                parts.append(self._rls_block("bundeslaender"))
                parts.append(self._rls_block(junction))

        # Stammdaten Bundesländer seeden.
        if normalize_states:
            seed = ",\n".join(f"    ({self._sql_str(b)})" for b in self.BUNDESLAENDER)
            parts.append(
                "insert into bundeslaender (name) values\n"
                + seed
                + "\non conflict (name) do nothing;\n\n"
            )

        # Haupttabelle: ein Bulk-Insert mit Upsert (atomar, keine Duplikate).
        value_rows = []
        for _, row in df.iterrows():
            values = [
                self._sql_str(row.get("name")),
                self._sql_str(row.get("website")),
                self._sql_num(row.get("zusatzbeitrag")),
                self._sql_str(row.get("geoffnet_in")),
                self._sql_bool(row.get("is_bundesweit")),
                self._sql_str(row.get("strasse")),
                self._sql_str(row.get("plz")),
                self._sql_str(row.get("ort")),
                self._sql_str(row.get("scraping_status")),
            ]
            value_rows.append("    (" + ", ".join(values) + ")")

        update_cols = ",\n".join(
            f"    {c} = excluded.{c}" for c in cols if c != "name"
        )
        parts.append(
            f"insert into {table_name} ({', '.join(cols)}) values\n"
            + ",\n".join(value_rows)
            + f"\non conflict (name) do update set\n{update_cols},\n"
            + "    updated_at = now();\n\n"
        )

        # n:m-Verknüpfung neu aufbauen. IDs sind zur Skriptzeit unbekannt,
        # daher Auflösung über die Namen (Kasse) bzw. den Bundesland-Namen.
        if normalize_states:
            pairs = []
            for _, row in df.iterrows():
                name = row.get("name")
                if pd.isna(name):
                    continue
                for state in self._parse_bundeslaender(
                    row.get("geoffnet_in"),
                    row.get("is_bundesweit"),
                    expand_bundesweit,
                ):
                    pairs.append(f"    ({self._sql_str(name)}, {self._sql_str(state)})")

            # Junction vollständig aus den aktuellen Daten neu aufbauen
            # (erfasst auch entfernte Zuordnungen bei erneutem Lauf).
            parts.append(f"truncate table {junction};\n\n")
            if pairs:
                parts.append(
                    f"insert into {junction} (krankenkasse_id, bundesland_id)\n"
                    f"select k.id, b.id\n"
                    f"from {table_name} k\n"
                    "cross join bundeslaender b\n"
                    "where (k.name, b.name) in (\n"
                    + ",\n".join(pairs)
                    + "\n);\n"
                )

        with open(target_path, "w", encoding="utf-8") as f:
            f.write("".join(parts))

        print(f"💾 SQL Upsert-Skript gespeichert unter: {target_path}")
        return target_path