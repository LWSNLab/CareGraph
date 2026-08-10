-- 0005_split_merged_insurer_row.sql — remove a row that was two insurers
--
-- The GKV parser treated "a line whose Zusatzbeitrag column contains a digit"
-- as the start of an entry. The SVLFG levies none — its cell reads "wird nicht
-- erhoben" — so its line looked like a continuation and was folded into the
-- entry above. The result was one row standing for two insurers, with both
-- names and both URLs concatenated:
--
--   name    = 'SKD BKK Sozialversicherung für Landwirtschaft, Forsten und Gartenbau (SVLFG)'
--   website = 'skd-bkk.dewww.svlfg.de'      (does not resolve)
--   ik      = '105508787'
--
-- Worse than a cosmetic defect: 105508787 is the **SVLFG's** IK, so an official
-- identifier was attached to a record presenting mostly as SKD BKK. Their real
-- numbers are 108833505 (SKD BKK) and 105508787 (SVLFG), per Schlüsselverzeichnis 8a.
--
-- The parser fix makes the loader emit two correct rows, but it cannot remove
-- the old one: the merged name no longer appears in the source, so no upsert
-- matches it, and the ingest role has no DELETE on care_infrastructure by
-- design (migration 0003). Hence this migration.
--
-- `ik_nummer` is UNIQUE, so this deletion is also a prerequisite: the SVLFG
-- cannot take 105508787 while the merged row still holds it.
--
-- Cascades remove the merged row's state links and its contribution-rate
-- history. That is intended — the rate was attributed to an entity that does
-- not exist. Re-running the insurer load restores SKD BKK's own 2.98 %.
--
-- Idempotent: on a database that never held the merged row this deletes nothing.

BEGIN;

DELETE FROM care_infrastructure
 WHERE type = 'krankenkasse'
   AND name = 'SKD BKK Sozialversicherung für Landwirtschaft, Forsten und Gartenbau (SVLFG)';

COMMIT;

-- Afterwards, re-run the insurer load so both insurers get their own IK:
--   python -m pipelines.run_load insurers --pdf pipelines/data/raw/gkv_liste_2026.pdf \
--       --gueltig-ab 2026-07-26 --allow-ik-regression
--
-- The flag is needed only while the Kostenträgerdateien sources are unreachable
-- (incomplete TLS chain on gkv-datenaustausch.de). It is safe here because
-- `_PRESERVE_IF_NULL` keeps already-resolved IKs, so a thinner resolve cannot
-- reduce coverage.
--
-- Verification (expects 93 insurers, no duplicate names, no glued URLs):
--   SELECT count(*) FROM care_infrastructure WHERE type = 'krankenkasse';
--   SELECT name, ik_nummer FROM care_infrastructure
--    WHERE name LIKE 'SKD BKK%' OR name LIKE 'Sozialversicherung%';
