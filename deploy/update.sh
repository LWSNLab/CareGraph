#!/bin/sh
# Pull the current release and restart what changed.
#
# The server fetches; nothing pushes to it. That means no inbound port beyond
# 80/443, and no server credentials stored at GitHub — a compromised workflow
# cannot reach this machine.
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

# Superseded layers would otherwise accumulate until the disk fills.
docker image prune -f >/dev/null

$COMPOSE ps
