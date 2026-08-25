-- 0003_least_privilege_roles.sql — separate roles for ingestion and the API
--
-- Until now both the loader and the (future) gateway would connect as the
-- database owner, i.e. with DROP TABLE rights for jobs that only ever read or
-- append. This migration creates the two roles from the security concept and
-- grants each exactly what its code actually executes:
--
--   caregraph_ingest  – the Python pipelines (write, never destructive)
--   caregraph_api     – the Go gateway (read only)
--
-- Privileges were derived from the statements in pipelines/load/postgres_loader.py
-- and internal/provider/repository.go, not assumed.
--
-- NO PASSWORDS ARE SET HERE. Migrations live in version control; credentials do
-- not. Set them out of band:
--
--   ALTER ROLE caregraph_ingest WITH PASSWORD '…';   -- from a secret manager
--   ALTER ROLE caregraph_api    WITH PASSWORD '…';
--
-- For local development `make db-roles-dev` sets throwaway passwords.

BEGIN;

-- Roles ---------------------------------------------------------------------
-- CREATE ROLE has no IF NOT EXISTS, so re-runs are guarded explicitly.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'caregraph_ingest') THEN
        CREATE ROLE caregraph_ingest LOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'caregraph_api') THEN
        CREATE ROLE caregraph_api LOGIN;
    END IF;
END$$;

GRANT CONNECT ON DATABASE caregraph TO caregraph_ingest, caregraph_api;
GRANT USAGE   ON SCHEMA public      TO caregraph_ingest, caregraph_api;

-- Ingestion role ------------------------------------------------------------
-- Upserts institutions. No DELETE: the pipeline never removes providers, and
-- withholding it means a bug cannot wipe the dataset.
GRANT SELECT, INSERT, UPDATE ON care_infrastructure TO caregraph_ingest;

-- State coverage is rebuilt per insurer, so this one does need DELETE.
GRANT SELECT, INSERT, DELETE ON krankenkasse_bundesland TO caregraph_ingest;

-- Contribution history is append-only *by design*. Granting only INSERT makes
-- the database enforce that, rather than trusting the loader to behave.
GRANT SELECT, INSERT ON zusatzbeitrag_historie TO caregraph_ingest;

-- Master data is seeded by migrations, so the pipeline only reads it.
GRANT SELECT ON bundeslaender TO caregraph_ingest;

-- API role ------------------------------------------------------------------
-- Read-only across the board: a compromised gateway cannot alter or drop data.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO caregraph_api;

-- Future tables -------------------------------------------------------------
-- Without this, the next migration's table would silently be unreachable for
-- both roles until someone remembered to grant it.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO caregraph_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO caregraph_ingest;

COMMIT;
