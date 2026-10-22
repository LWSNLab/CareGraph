#!/bin/sh
# deploy/lib.sh — shared by the deployment scripts. Not run on its own.
#
# POSIX sh, like update.sh: the server has no bash guarantee, and nothing here
# needs one.

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/deploy/state"

COMPOSE="docker compose -f $ROOT/docker-compose.yml -f $ROOT/docker-compose.prod.yml --profile app --profile edge"

say()  { printf '\n== %s\n' "$*"; }
fail() { printf '\nFAILED: %s\n' "$*" >&2; }

# The file carries the database passwords and the Typesense key. Whoever can
# read it owns every API key ever issued.
require_env() {
    [ -f "$ROOT/.env" ] || {
        fail "$ROOT/.env is missing. Create it with: make env-prod DOMAIN=… EMAIL=…"
        exit 1
    }
    mode="$(stat -c '%a' "$ROOT/.env" 2>/dev/null || stat -f '%Lp' "$ROOT/.env")"
    case "$mode" in
        600 | 400) ;;
        *)
            fail ".env has mode $mode, expected 600. Fix with: chmod 600 $ROOT/.env"
            exit 1
            ;;
    esac
    mkdir -p "$STATE"
}

# Deployment is by digest, never by tag.
#
# Not pedantry: a tag can point at a different image tomorrow than it did when
# it was tested, so `latest` names a moving target and `v0.2.0` names one that
# could be re-pushed. A digest is the content. Refused rather than warned about,
# because a warning in a deploy log is read after the deploy.
require_digest() {
    case "$1" in
        *@sha256:*) ;;
        *)
            fail "'$1' is not a digest. Deployment is by digest only."
            fail "Resolve one with: deploy/resolve.sh v0.2.0"
            exit 1
            ;;
    esac
}

# The image says which commit it was built from; the checkout says which commit
# it is. If they disagree, the migrations about to be applied are not the ones
# this image expects — which is the rollback hazard, reachable by following the
# runbook. Refused, not reported.
require_matching_checkout() {
    image="$1"

    image_revision="$(docker image inspect "$image" \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' 2>/dev/null || true)"

    if [ -z "$image_revision" ] || [ "$image_revision" = "<no value>" ]; then
        fail "$image carries no org.opencontainers.image.revision label."
        fail "Built outside the release workflow? Deploying it would mean guessing"
        fail "which migrations belong to it."
        exit 1
    fi

    checkout_revision="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
    if [ -z "$checkout_revision" ]; then
        fail "$ROOT is not a git checkout — cannot tell whether the migrations match."
        exit 1
    fi

    if [ "$image_revision" != "$checkout_revision" ]; then
        fail "The image and the checkout are different revisions."
        fail "  image:    $image_revision"
        fail "  checkout: $checkout_revision"
        fail ""
        fail "Migrations come from the checkout. Applying this checkout's against"
        fail "that image is how an older binary meets a newer schema."
        fail "Fix with: git -C $ROOT fetch --tags && git -C $ROOT checkout $image_revision"
        exit 1
    fi

    say "Image and checkout agree on $(echo "$checkout_revision" | cut -c1-7)"
}

# Readiness, read from the container's own healthcheck.
#
# The image declares HEALTHCHECK ["/api", "-healthcheck"], which probes /readyz
# — so the verdict already exists and asking Docker for it needs no shell in a
# distroless image.
wait_for_healthy() {
    attempts="${1:-30}"
    i=0
    while [ "$i" -lt "$attempts" ]; do
        i=$((i + 1))
        container="$($COMPOSE ps -q api 2>/dev/null | head -1)"

        if [ -z "$container" ]; then
            sleep 2
            continue
        fi

        status="$(docker inspect --format '{{ .State.Health.Status }}' "$container" 2>/dev/null || echo unknown)"
        case "$status" in
            healthy)
                say "Healthy after ${i} check(s)"
                return 0
                ;;
            unhealthy)
                # Keep waiting: the healthcheck has retries, and `unhealthy`
                # during the start period is the normal first answer.
                ;;
        esac
        sleep 2
    done

    fail "The API did not become healthy within $((attempts * 2))s. Last 40 lines:"
    $COMPOSE logs --tail=40 api >&2 || true
    return 1
}

start_with() {
    api_image="$1"
    ingest_image="${2:-}"

    # --no-build: there is no source tree worth building from on a server, and
    # without it a failed pull silently becomes a local build whose result
    # nobody tested.
    if [ -n "$ingest_image" ]; then
        CAREGRAPH_API_IMAGE="$api_image" \
        CAREGRAPH_INGEST_IMAGE="$ingest_image" \
            $COMPOSE pull api caddy

        CAREGRAPH_API_IMAGE="$api_image" \
        CAREGRAPH_INGEST_IMAGE="$ingest_image" \
            $COMPOSE up -d --no-build
    else
        CAREGRAPH_API_IMAGE="$api_image" \
            $COMPOSE pull api caddy

        CAREGRAPH_API_IMAGE="$api_image" \
            $COMPOSE up -d --no-build
    fi
}

# Check the Caddyfile and reload it into a running Caddy.
#
# Necessary because the Caddyfile is bind-mounted: its contents are not part of
# the container's description, so compose sees no change and recreates nothing.
# The file would sit updated on disk while Caddy served the old one.
#
# Validate first: a broken Caddyfile taken live removes the only public service.
reload_caddy() {
    say "Checking the Caddy configuration"

    caddy_state="$($COMPOSE ps --format '{{.State}}' caddy 2>/dev/null | head -1 || true)"

    if [ "$caddy_state" = "running" ]; then
        # Validated inside the running container, so the version that checks is
        # the version that will serve.
        if ! $COMPOSE exec -T caddy caddy validate --config /etc/caddy/Caddyfile; then
            fail "The Caddyfile is invalid. Nothing was deployed."
            exit 1
        fi
        say "Reloading Caddy"
        $COMPOSE exec -T caddy caddy reload --config /etc/caddy/Caddyfile
        cp "$ROOT/deploy/Caddyfile" "$STATE/Caddyfile.deployed"
        return 0
    fi

    say "Caddy is not running yet — it will start with this configuration"
}

record() {
    new="$1"
    previous="${2:-}"

    printf '%s\n' "$new" > "$STATE/digest.current"

    # Only overwrite when the digest actually changed. Re-deploying the same
    # image would otherwise put the same value in both files, and the rollback
    # would lead nowhere.
    if [ -n "$previous" ] && [ "$previous" != "$new" ]; then
        printf '%s\n' "$previous" > "$STATE/digest.previous"
    fi
}

# Resolve a tag to the digest it points at, right now.
#
# RepoDigests rather than `buildx imagetools`: a server installed from the
# docker apt repository has the compose plugin but not necessarily buildx, and
# the pull has to happen anyway.
resolve_digest() {
    ref="$1"
    docker pull --quiet "$ref" >/dev/null || {
        fail "Could not pull $ref — does the tag exist, and is the package public?"
        exit 1
    }
    digest="$(docker image inspect "$ref" --format '{{ index .RepoDigests 0 }}' 2>/dev/null || true)"
    [ -n "$digest" ] || {
        fail "$ref reports no repo digest."
        exit 1
    }
    printf '%s\n' "$digest"
}

image_label() {
    docker image inspect "$1" --format "{{ index .Config.Labels \"$2\" }}" 2>/dev/null || true
}

current_digest()  { cat "$STATE/digest.current" 2>/dev/null || true; }
previous_digest() { cat "$STATE/digest.previous" 2>/dev/null || true; }
