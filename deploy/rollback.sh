#!/bin/sh
# deploy/rollback.sh [api-image@sha256:…]
#
# Goes back to the previously deployed digest, or to one given explicitly.
# One command on the server, needing neither GitHub nor a new build.
#
# **What this does not do: undo migrations.** Rolling back the image returns the
# binary, not the schema. Going back to an older schema is a restore from
# backup (`make restore`), which is a different and much heavier operation.
#
# So before running this, the question worth asking is whether the new image is
# the cause at all. A database, Redis or Typesense that is down looks the same
# from here — and in that case the old image will be just as unhealthy, having
# cost a second deployment to find out.

set -eu

. "$(dirname "$0")/lib.sh"

require_env

TARGET="${1:-$(previous_digest)}"

[ -n "$TARGET" ] || {
    fail "No previous digest recorded, and none given."
    fail "List what the registry holds, then name one:"
    fail "  deploy/resolve.sh v0.1.1"
    fail "  $0 ghcr.io/lwsnlab/caregraph-api@sha256:…"
    exit 1
}

require_digest "$TARGET"

CURRENT="$(current_digest)"

say "Rolling back from ${CURRENT:-unknown}"
say "                to $TARGET"

docker pull --quiet "$TARGET" >/dev/null

# The same gate as on the way forward. A rollback is the moment most likely to
# put an old image next to a new schema, so the check matters more here, not
# less — it will refuse until the checkout is moved back too.
require_matching_checkout "$TARGET"

reload_caddy
start_with "$TARGET"

if wait_for_healthy 30; then
    # The digest rolled back *from* becomes the previous one, so a second call
    # goes forward again rather than into nothing.
    record "$TARGET" "$CURRENT"
    say "Rolled back. Running: $TARGET"
    say "Verify from outside with: deploy/verify.sh"
    exit 0
fi

fail "The previous image is not healthy either."
fail "That points away from the image and towards a dependency — the database,"
fail "Redis, Typesense, the network, or the environment file."
fail "Start with: $COMPOSE ps  and  $COMPOSE logs --tail=50 api"
exit 1
