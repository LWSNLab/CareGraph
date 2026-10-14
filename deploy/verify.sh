#!/bin/sh
# deploy/verify.sh [host]
#
# Checks the deployment from outside, over the path users actually take.
#
# Two kinds of check, and the second is the one that matters:
#
#   present  — TLS terminates, the API answers, the contract is served
#   absent   — the database, Redis, Typesense and the API's own port are NOT
#              reachable from off the machine
#
# Assertions about absence are the ones that quietly stop being true. The
# compose overlay binds Postgres to loopback and leaves Redis and Typesense
# unpublished; nothing until now checked that it stayed that way. A `!override`
# removed by a later edit, a port added for one debugging session — neither
# shows up in a readiness probe.
#
# `/readyz` is deliberately NOT in the absent list. It is public by design here
# and documented as such; the operator is meant to be able to curl it.

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
bad()  { printf '  ❌ %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }

FAILURES=0

DOMAIN="${1:-$(sed -n 's/^CAREGRAPH_DOMAIN=//p' "$ROOT/.env" 2>/dev/null | tr -d '"')}"
[ -n "$DOMAIN" ] || {
    printf 'No domain. Pass one, or set CAREGRAPH_DOMAIN in .env.\n' >&2
    exit 1
}

printf '\n== Reachable, over TLS\n'

code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://$DOMAIN/readyz" 2>/dev/null || echo 000)"
case "$code" in
    200) ok "/readyz answers 200 — every dependency reachable" ;;
    503) bad "/readyz answers 503 — a required dependency is down (the service itself is up)" ;;
    000) bad "https://$DOMAIN/readyz unreachable: no TLS, no certificate, or DNS points elsewhere" ;;
    *)   bad "/readyz answers $code" ;;
esac

code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://$DOMAIN/openapi.yaml" 2>/dev/null || echo 000)"
[ "$code" = "200" ] && ok "the contract is served" || bad "/openapi.yaml answers $code"

# Unauthenticated /v1 must be refused — otherwise the data is public and the
# rate limiter has nothing to key on.
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 \
    "https://$DOMAIN/v1/infrastructure/near?lat=52.52&lng=13.405" 2>/dev/null || echo 000)"
[ "$code" = "401" ] && ok "/v1 without a key answers 401" || bad "/v1 without a key answers $code, expected 401"

printf '\n== Not reachable from outside\n'

# curl exit codes say what happened at the TCP level:
#   7  connection refused   — closed
#   28 timed out            — filtered
# anything else means something answered, which for these ports is the finding.
closed() {
    label="$1"
    port="$2"
    curl -sS --max-time 6 "http://$DOMAIN:$port/" >/dev/null 2>&1
    status=$?
    case "$status" in
        7|28) ok "$label (port $port) is closed" ;;
        0)    bad "$label (port $port) ANSWERS from outside" ;;
        *)    ok "$label (port $port) is not usable from outside (curl $status)" ;;
    esac
}

closed "PostgreSQL" "${POSTGRES_PORT:-5433}"
closed "Typesense"  "8108"
closed "Redis"      "6379"
closed "the API, bypassing Caddy" "${CAREGRAPH_PORT:-8080}"

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    printf 'All checks passed.\n'
    exit 0
fi
printf '%d check(s) failed.\n' "$FAILURES" >&2
exit 1
