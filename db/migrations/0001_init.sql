-- 0001_init.sql — CareGraph core schema (PostgreSQL 16 + PostGIS 3.4)
-- Reference: docs (LWSNLab/CareGraph_Doc) architecture/data-schema.md

CREATE EXTENSION IF NOT EXISTS postgis;

-- Provider classifications
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'provider_type') THEN
        CREATE TYPE provider_type AS ENUM (
            'krankenkasse',
            'pflegedienst_ambulant',
            'pflegeheim_stationaer',
            'pflegestuetzpunkt'
        );
    END IF;
END$$;

-- Core infrastructure table (one row per institution)
CREATE TABLE IF NOT EXISTS care_infrastructure (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ik_nummer VARCHAR(9) UNIQUE,                   -- Official 9-digit Institution Code
    type provider_type NOT NULL,
    name VARCHAR(255) NOT NULL,
    parent_organization VARCHAR(255),
    website VARCHAR(255),

    -- Structured address data
    strasse VARCHAR(255),
    plz VARCHAR(10) NOT NULL,
    ort VARCHAR(100) NOT NULL,
    bundesland VARCHAR(50),

    -- PostGIS spatial data (WGS84 / SRID 4326)
    location GEOGRAPHY(Point, 4326),

    -- Dynamic metadata store
    details JSONB NOT NULL DEFAULT '{}'::jsonb,

    scraping_status VARCHAR(50) DEFAULT 'raw',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_care_infra_location ON care_infrastructure USING GIST (location);
CREATE INDEX IF NOT EXISTS idx_care_infra_type_plz ON care_infrastructure (type, plz);
CREATE INDEX IF NOT EXISTS idx_care_infra_details  ON care_infrastructure USING GIN (details);

-- 16 German federal states (master data)
CREATE TABLE IF NOT EXISTS bundeslaender (
    id   SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

INSERT INTO bundeslaender (name) VALUES
    ('Baden-Württemberg'), ('Bayern'), ('Berlin'), ('Brandenburg'),
    ('Bremen'), ('Hamburg'), ('Hessen'), ('Mecklenburg-Vorpommern'),
    ('Niedersachsen'), ('Nordrhein-Westfalen'), ('Rheinland-Pfalz'),
    ('Saarland'), ('Sachsen'), ('Sachsen-Anhalt'),
    ('Schleswig-Holstein'), ('Thüringen')
ON CONFLICT (name) DO NOTHING;

-- n:m link between an insurer and the states it is open in
CREATE TABLE IF NOT EXISTS krankenkasse_bundesland (
    krankenkasse_id UUID     NOT NULL REFERENCES care_infrastructure(id) ON DELETE CASCADE,
    bundesland_id   SMALLINT NOT NULL REFERENCES bundeslaender(id)       ON DELETE CASCADE,
    PRIMARY KEY (krankenkasse_id, bundesland_id)
);

CREATE INDEX IF NOT EXISTS idx_kk_bl_bundesland ON krankenkasse_bundesland (bundesland_id);

-- Append-only history of supplementary contribution rates
CREATE TABLE IF NOT EXISTS zusatzbeitrag_historie (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    krankenkasse_id UUID NOT NULL REFERENCES care_infrastructure(id) ON DELETE CASCADE,
    gueltig_ab      DATE NOT NULL,                    -- "Stand" / valid-from date of the list
    zusatzbeitrag   NUMERIC(4,2) NOT NULL,
    quelle          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (krankenkasse_id, gueltig_ab)
);

CREATE INDEX IF NOT EXISTS idx_zb_hist_kk_datum ON zusatzbeitrag_historie (krankenkasse_id, gueltig_ab DESC);

-- Convenience view: the current contribution rate per insurer (latest row)
CREATE OR REPLACE VIEW zusatzbeitrag_aktuell AS
SELECT DISTINCT ON (krankenkasse_id)
       krankenkasse_id, gueltig_ab, zusatzbeitrag
FROM   zusatzbeitrag_historie
ORDER  BY krankenkasse_id, gueltig_ab DESC;
