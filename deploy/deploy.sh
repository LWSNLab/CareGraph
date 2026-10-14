#!/bin/sh
# deploy/deploy.sh <api-image@sha256:…> [ingest-image@sha256:…]
#
# Puts one specific image live and records which one it replaced.
#
# Called by update.sh from the timer, and runnable by hand — the way back must
# not depend on GitHub, or the one moment it is needed is the one moment it is
# unavailable.
#
# **This does not roll back on its own, deliberately.** Rolling back the image
# does not roll back a migration, and CareGraph's migrations are not additive:
# 0002 adds `source_id NOT NULL`, 0005 deletes rows. An older binary against
# this schema fails on insert. So a failed deploy stops, says what to do, and
# leaves the decision to a person — rollback.sh makes it one command.
#
# Automatic rollback becomes safe once migrations are backward-compatible by
# policy, not before. That order is the point: the mechanism without the
# guarantee is worse than neither, because it looks like safety.

set -eu

. "$(dirname "$0")/lib.sh"

API_IMAGE="${1:-}"
INGEST_IMAGE="${2:-}"

[ -n "$API_IMAGE" ] || {
    fail "No image given. Usage: $0 ghcr.io/lwsnlab/caregraph-api@sha256:… [ingest@sha256:…]"
    exit 1
}

require_digest "$API_IMAGE"
[ -z "$INGEST_IMAGE" ] || require_digest "$INGEST_IMAGE"
require_env

PREVIOUS="$(current_digest)"

say "Currently live: ${PREVIOUS:-nothing deployed yet}"
say "Deploying:      $API_IMAGE"

# Pull before inspecting: the labels are read from the local image, and the
# revision check below is the gate that decides whether this may go live at all.
say "Pulling"
docker pull --quiet "$API_IMAGE" >/dev/null
[ -z "$INGEST_IMAGE" ] || docker pull --quiet "$INGEST_IMAGE" >/dev/null

require_matching_checkout "$API_IMAGE"

# Configuration first. A broken Caddyfile should stop the deploy before the
# application is touched, not after.
reload_caddy

start_with "$API_IMAGE" "$INGEST_IMAGE"

# Migrations come from the checkout, which the revision check above has already
# tied to this image. The compose mount only applies them when the volume is
# empty, so an update would otherwise leave an existing database on the old
# schema — the API starts, its readiness probe passes on a plain ping, and every
# query then fails.
say "Applying migrations"
make -C "$ROOT" migrate

say "Waiting for the API to report itself healthy"

if wait_for_healthy 30; then
    record "$API_IMAGE" "$PREVIOUS"
    say "Deployed. Running: $API_IMAGE"
    say "Verify from outside with: deploy/verify.sh"
    exit 0
fi

fail "The new image did not become healthy."
fail ""

if [ -z "$PREVIOUS" ]; then
    fail "There is no previous deployment to go back to. The stack is left as it"
    fail "is — inspect it rather than redeploying blindly."
    exit 1
fi

fail "Not rolling back automatically: the migrations above have already been"
fail "applied, and this project's are not backward-compatible. An older binary"
fail "against this schema can fail in ways that are harder to see than a 503."
fail ""
fail "To go back deliberately:"
fail "  deploy/rollback.sh"
fail ""
fail "That returns to $PREVIOUS. Check first whether the cause is the image at"
fail "all — a database, Redis or Typesense that is down looks identical from here."
exit 1
