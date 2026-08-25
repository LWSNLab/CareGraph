-- 0004_absolute_website_urls.sql — make `website` an absolute URL
--
-- The GKV parser strips the scheme on purpose: the address scraper wants a bare
-- host to build Impressum paths from. That internal form leaked all the way to
-- the API, where `GET /v1/infrastructure/{ik_nummer}` answered
-- `"website": "hek.de"` — not something a client can follow.
--
-- The loader now normalises at write time (pipelines/common/normalize.py), so
-- new and re-loaded rows are correct. This migration fixes the rows already in
-- the table, which is 103 values: all 92 insurers plus 11 OSM providers whose
-- `website` tag had no scheme either.
--
-- Scheme choice mirrors the loader exactly. https for everything except the two
-- hosts measured (2026-08-10) to have no listening HTTPS port. Hosts that serve
-- HTTPS with a bad certificate are NOT downgraded: a broken certificate is the
-- site's problem, and http would quietly weaken the connection we recommend.

BEGIN;

-- 1 ------------------------------------------------------- http-only hosts
UPDATE care_infrastructure
   SET website = 'http://' || website,
       updated_at = now()
 WHERE website IS NOT NULL
   AND website NOT LIKE 'http://%'
   AND website NOT LIKE 'https://%'
   AND (
        split_part(regexp_replace(lower(website), '^www\.', ''), '/', 1)
          IN ('seniorenheim-eggmuehl.brk.de', 'suedzucker-bkk.de')
       );

-- 2 ------------------------------------------------------------ everything else
UPDATE care_infrastructure
   SET website = 'https://' || website,
       updated_at = now()
 WHERE website IS NOT NULL
   AND website NOT LIKE 'http://%'
   AND website NOT LIKE 'https://%';

COMMIT;

-- Verification (should return no rows):
--   SELECT type, count(*) FROM care_infrastructure
--    WHERE website IS NOT NULL AND website NOT LIKE 'http%' GROUP BY type;
