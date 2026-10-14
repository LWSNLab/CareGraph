#!/bin/sh
# deploy/resolve.sh [tag]
#
# Turns a tag into the digest it currently points at, so deploying by digest
# does not require anyone to remember a docker incantation.
#
#   deploy/resolve.sh v0.2.0
#   deploy/resolve.sh            # latest
#
# The resolution happens once, here. After this the digest is what travels, and
# it cannot change meaning on the way.

set -eu

. "$(dirname "$0")/lib.sh"

TAG="${1:-latest}"
REGISTRY="${CAREGRAPH_REGISTRY:-ghcr.io/lwsnlab}"

API="$(resolve_digest "$REGISTRY/caregraph-api:$TAG")"
INGEST="$(resolve_digest "$REGISTRY/caregraph-ingest:$TAG")"

REVISION="$(image_label "$API" org.opencontainers.image.revision)"
VERSION="$(image_label "$API" org.opencontainers.image.version)"

cat <<EOF

Tag $TAG currently resolves to:

  api:      $API
  ingest:   $INGEST
  version:  ${VERSION:-unknown}
  revision: ${REVISION:-unknown}

Deploy it with:

  git fetch --tags && git checkout ${REVISION:-<revision>}
  deploy/deploy.sh '$API' '$INGEST'

The checkout comes first: deploy.sh refuses an image whose revision does not
match it, because the migrations come from the checkout.
EOF
