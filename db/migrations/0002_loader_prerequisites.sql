-- 0002_loader_prerequisites.sql — make care_infrastructure loadable (story E1-S4)
--
-- Two changes, both driven by what the real ingested data looks like:
--
-- 1. `plz`/`ort` become nullable. 30% of the 7,522 OSM provider records carry
--    no address at all, but every one of them has a name and coordinates.
--    For a spatial API `location` is the load-bearing field, so rejecting a
--    third of the dataset over a missing postcode would be the worse trade.
--    E1-S3 backfills these by reverse geocoding.
--
-- 2. A `source_id` upsert key is added. Providers have no IK-Nummer, and names
--    are not unique, so re-running ingestion had no way to recognise a record
--    it had already written. `source_id` namespaces the external identifier per
--    source ("osm:node/722542669", "ik:108616568") and makes the load idempotent.

BEGIN;

-- 1 ---------------------------------------------------------------- addresses
ALTER TABLE care_infrastructure ALTER COLUMN plz DROP NOT NULL;
ALTER TABLE care_infrastructure ALTER COLUMN ort DROP NOT NULL;

-- 2 --------------------------------------------------------------- upsert key
ALTER TABLE care_infrastructure ADD COLUMN IF NOT EXISTS source_id TEXT;

-- Safe on an already-populated table: give existing rows a synthetic key
-- before the NOT NULL constraint is applied.
UPDATE care_infrastructure
   SET source_id = 'legacy:' || id::text
 WHERE source_id IS NULL;

ALTER TABLE care_infrastructure ALTER COLUMN source_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_care_infra_source_id
    ON care_infrastructure (source_id);

COMMIT;
