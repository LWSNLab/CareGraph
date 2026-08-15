-- 0007_hospital_provider_type.sql — hospitals as a care_infrastructure type (E1-S9)
--
-- Adds `krankenhaus` to the provider_type enum so the Bundes-Klinik-Atlas export
-- (§ 135d SGB V) can be loaded alongside the care providers. This is what takes
-- the dataset across the SGB V / SGB XI boundary a patient actually crosses:
-- hospital → rehab → outpatient care.
--
-- ADD VALUE cannot run inside a transaction block in PostgreSQL, hence no
-- explicit BEGIN/COMMIT here. `IF NOT EXISTS` keeps the migration re-runnable.

ALTER TYPE provider_type ADD VALUE IF NOT EXISTS 'krankenhaus';
