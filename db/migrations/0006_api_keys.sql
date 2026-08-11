-- 0006_api_keys.sql — API-key store for the public gateway (story E3-S4)
--
-- Until now `X-API-Key` was only checked for *presence*: any non-empty value
-- passed. This adds the store that makes it authentication.
--
-- Key format: `cg_<key_id>_<secret>`
--
-- The split is what makes verification affordable. Argon2id is deliberately slow
-- (tens of milliseconds), so the presented key cannot be hashed against every
-- stored row. `key_id` is a public, indexed identifier that selects exactly one
-- row; only then is the secret verified against that row's hash. One indexed
-- lookup plus one Argon2id verification, regardless of how many keys exist.
--
-- Consequence worth knowing: `key_id` is not a secret and may appear in logs.
-- The secret half never touches the database in plaintext.

BEGIN;

-- Tiers from docs/architecture/security.md §2.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'api_tier') THEN
        CREATE TYPE api_tier AS ENUM ('community', 'enterprise');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS api_key (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Public lookup half of the key. Indexed, not secret.
    key_id      TEXT NOT NULL UNIQUE,

    -- Argon2id of the secret half, in the standard encoded form
    -- ($argon2id$v=19$m=...,t=...,p=...$salt$hash) so the parameters travel with
    -- the value and can be raised later without a migration.
    secret_hash TEXT NOT NULL,

    -- Who the key was issued to. Free text; the audit trail, not a foreign key.
    name        TEXT NOT NULL,

    tier        api_tier NOT NULL DEFAULT 'community',

    -- Per-key override for the tier default, for the "custom SLA" case. NULL
    -- means "use the tier's limit".
    rate_limit_per_min INTEGER CHECK (rate_limit_per_min IS NULL OR rate_limit_per_min > 0),

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Revocation is a timestamp, not a DELETE: which key was used and when it
    -- stopped being valid is exactly what you want during an incident. The
    -- gateway has no DELETE on this table anyway (migration 0003's posture).
    revoked_at  TIMESTAMPTZ
);

COMMENT ON TABLE api_key IS
    'B2B API keys. key_id is public and indexed; secret_hash is Argon2id of the secret half.';

-- Only unrevoked keys are ever looked up.
CREATE INDEX IF NOT EXISTS idx_api_key_active
    ON api_key (key_id) WHERE revoked_at IS NULL;

-- The gateway reads keys and nothing more. Issuing and revoking are owner-level
-- operations, done through cmd/apikey, so a compromised gateway cannot mint
-- itself a key or silence an audit trail.
GRANT SELECT ON api_key TO caregraph_api;

COMMIT;
