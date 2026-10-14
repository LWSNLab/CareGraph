#!/bin/sh
# deploy/update.sh
#
# The automated path: find what the followed tag points at now, move the
# checkout to the matching revision, and deploy that digest.
#
# The server fetches; nothing pushes to it. No inbound port beyond 80/443 and no
# server credentials at GitHub — a compromised workflow cannot reach this
# machine. The cost is up to one timer interval of delay.
#
# Run from a timer (see deploy/README.md) or by hand.
#
# What changed from following `latest` directly: the tag is resolved to a digest
# **once**, here, and that digest is what gets deployed and recorded. Before,
# compose resolved `latest` itself on every run, so the thing that went live was
# whatever the tag meant at that second — and no record was kept of which image
# that had been.

set -eu

. "$(dirname "$0")/lib.sh"

require_env

FOLLOW="${CAREGRAPH_FOLLOW:-latest}"
REGISTRY="${CAREGRAPH_REGISTRY:-ghcr.io/lwsnlab}"

say "Checking what $FOLLOW points at"

API="$(resolve_digest "$REGISTRY/caregraph-api:$FOLLOW")"
INGEST="$(resolve_digest "$REGISTRY/caregraph-ingest:$FOLLOW")"

CURRENT="$(current_digest)"

# The timer runs every ten minutes. Without this the stack would be re-deployed
# and the migrations re-applied on every tick, which works — they are
# re-runnable — but fills the log with activity that did nothing.
if [ "$API" = "$CURRENT" ]; then
    say "Already running $API — nothing to do"
    exit 0
fi

REVISION="$(image_label "$API" org.opencontainers.image.revision)"
[ -n "$REVISION" ] || {
    fail "$API carries no revision label; refusing to guess which migrations belong to it."
    exit 1
}

# The checkout supplies the compose files, the Caddyfile and the migrations, so
# it has to be the revision this image was built from. Fetched and checked out
# explicitly rather than pulled: following a branch would move the checkout to
# whatever is newest, which is not necessarily what the registry is serving.
say "Moving the checkout to $(echo "$REVISION" | cut -c1-7)"
git -C "$ROOT" fetch --quiet --tags origin
git -C "$ROOT" checkout --quiet --detach "$REVISION"

# Superseded layers would otherwise accumulate until the disk fills. Before the
# deploy, so a failure leaves the previous image in place to roll back to.
docker image prune -f >/dev/null

exec "$ROOT/deploy/deploy.sh" "$API" "$INGEST"
