"""Web scrapers for care-provider directories and insurer sites.

Modules
-------
`osm_provider_scraper`
    Care providers (Pflegedienste, Pflegeheime, Pflegestützpunkte) from
    OpenStreetMap via the Overpass API — the nationwide base source for
    story E1-S2.
`address_scraper`
    Impressum/contact address enrichment for the statutory health insurers
    parsed by `pipelines.parsers.gkv_parser` (story E1-S1).

Source choice, briefly
----------------------
The obvious insurer portals are NOT scraped. `pflegelotse.de` disallows its
stationary result page and quality reports in `robots.txt`, and
`pflege.aok.de` disallows every URL carrying a query string — which is its
entire search. Both are additionally protected databases (§ 87a UrhG).
OpenStreetMap is openly licensed (ODbL) and intended for bulk access.

Respect each source's robots.txt and terms — see the documentation repo,
`docs/legal/data-licensing.md`.
"""
