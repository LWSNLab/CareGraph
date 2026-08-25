#!/bin/sh
# Pull the current release, apply it, and check that it actually works.
#
# The server fetches; nothing pushes to it. No inbound port beyond 80/443, and no
# server credentials at GitHub — a compromised workflow cannot reach this machine.
#
# Run from a timer (see deploy/README.md) or by hand after a merge to main.

set -eu

cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile app --profile edge"

# The compose files, Caddyfile and migrations come from the checkout; only the
# application images come from the registry. --ff-only so a dirty server refuses
# rather than silently merging.
git pull --ff-only

$COMPOSE pull
$COMPOSE up -d

# The compose mount only runs migrations when the volume is empty, so an update
# would otherwise leave an existing database on the old schema — the API starts,
# its readiness probe passes on a plain ping, and every query then fails. All
# migrations are re-runnable; CI applies them twice to keep that true.
make migrate

# Superseded layers would otherwise accumulate until the disk fills.
docker image prune -f >/dev/null

# Without this an update counts as successful the moment a container starts. The
# check goes over the public name rather than 127.0.0.1, because that is the path
# users take: a container can be perfectly healthy while the certificate, the
# proxy or DNS is not.
domain=$(sed -n 's/^CAREGRAPH_DOMAIN=//p' .env | tr -d '"')
if [ -z "$domain" ]; then
    echo "no CAREGRAPH_DOMAIN in .env — skipping the public check" >&2
    exit 0
fi

echo "verifying https://$domain/readyz"
for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://$domain/readyz" || true)
    case "$code" in
        200) echo "update verified: the service answers over TLS"; exit 0 ;;
        503) echo "reachable, but a required dependency is down" >&2; break ;;
    esac
    sleep 5
done

echo "UPDATE NOT VERIFIED — https://$domain/readyz last answered '$code'" >&2
echo "Recent API log:" >&2
$COMPOSE logs --tail=50 api >&2
echo >&2
echo "The new images are running. To go back to a known build, set" >&2
echo "CAREGRAPH_TAG=v<previous> in .env and re-run this script." >&2
echo "Rolling back is deliberate rather than automatic: migrations have already" >&2
echo "been applied, and an older image against a newer schema can be worse." >&2
exit 1
