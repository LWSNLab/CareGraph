# Deploying CareGraph

A single-host deployment: one VPS, Caddy in front, everything else on a private
container network. Sized against measured usage — the whole stack idles at about
375 MiB, so the smallest plan anywhere is enough.

Nothing here needs a Go toolchain or `uv` on the server. The application images
come from GHCR, built by CI on every merge to `main`; the checkout supplies only
the compose files, the Caddyfile and the migrations.

## 1. The machine

Any current Debian or Ubuntu. Create an unprivileged user and install Docker with
the compose plugin.

```bash
adduser caregraph && usermod -aG docker caregraph
```

Open exactly three ports. The database, Redis and Typesense are never published
off the machine, and the API only via Caddy:

```bash
ufw default deny incoming && ufw allow 22,80,443/tcp && ufw enable
```

## 2. DNS

Point an A record (and AAAA if you have IPv6) at the server **before** starting
the stack. Caddy asks Let's Encrypt for a certificate on first boot, and that
fails if the name does not yet resolve.

```
api.caregraph.de.  A  <server-ip>
```

Let's Encrypt rate-limits failed issuance, so it is worth confirming with `dig`
first rather than restarting the stack repeatedly.

## 3. The checkout

Once the repository is public this needs no credentials at all:

```bash
git clone https://github.com/LWSNLab/CareGraph.git && cd CareGraph
```

While it is still private, generate a **read-only deploy key** on the server and
paste the *public* half into the repository under Settings → Deploy keys, leaving
"Allow write access" unticked. A deploy key grants exactly one repository; a
personal access token would grant everything the account can reach.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/caregraph_deploy -N ''
cat ~/.ssh/caregraph_deploy.pub          # paste this into GitHub
git clone git@github.com:LWSNLab/CareGraph.git && cd CareGraph
```

## 4. Secrets

```bash
make env-prod DOMAIN=api.caregraph.de EMAIL=you@example.org
```

Writes `.env` with generated passwords, mode 0600. It refuses to overwrite an
existing file — passwords for a running database are not something to regenerate
by accident.

Keep a copy somewhere safe. Losing `.env` means losing access to the database,
and `make env-prod` cannot reconstruct it.

## 5. Start

```bash
make up            # database, Redis, Typesense — migrations run on first boot
make db-roles      # apply the generated passwords to the least-privilege roles

docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile app --profile edge pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile app --profile edge up -d
```

Add `--build` instead of the `pull` if nothing has been merged to `main` yet, so
no image exists in the registry — that builds on the machine.

Caddy fetches a certificate on first boot; watch it with
`docker compose logs -f caddy`.

Then check that the API considers itself ready — this is the container's own
healthcheck, so `docker compose ps` shows the same verdict:

```bash
curl https://api.caregraph.de/readyz
```

`degraded` means an optional dependency is down and the API still serves.
`unavailable` with a 503 means Postgres is unreachable.

## 6. Data

The database is empty until you load it. From the published archive:

```bash
make dataset-import FILE=dist/caregraph-providers-YYYY-MM-DD.tar.gz
make search-sync
```

The archive contains care providers only. Insurers need the GKV list PDF
(`make load-insurers`), hospitals the Bundes-Klinik-Atlas export — and until the
Standortverzeichnis answers the redistribution question, hospitals should not go
into anything published.

## 7. A key

```bash
make apikey-dev            # or: go run ./cmd/apikey issue --name "Someone" --tier community
```

Printed once and stored only as an Argon2id hash. There is no recovery — a lost
key is revoked and reissued.

## 8. Backups

**Do this before there are users.** The dataset archive holds providers only; the
`api_key` table is in no archive, so a lost volume invalidates every key ever
issued.

```bash
make backup                      # → backups/caregraph-<timestamp>.sql.gz
make restore FILE=backups/...    # replaces the database
```

A dump of the current data is about 1.4 MB. Nightly, keeping 30 days:

```cron
15 3 * * *  cd /home/caregraph/CareGraph && make backup >/dev/null 2>&1
20 3 * * *  find /home/caregraph/CareGraph/backups -name '*.sql.gz' -mtime +30 -delete
```

The dump carries password hashes and the whole dataset. Treat it like `.env`, and
copy it off the machine — a backup on the disk you are protecting against is not
one.

## Updating

**A merge into `main` is a release.** `.github/workflows/release.yml` builds both
images and pushes them to GHCR, tagged `latest` and `sha-<commit>`. The server
fetches them — nothing pushes to the server, so no inbound port beyond 80/443 and
no server credentials at GitHub.

```bash
deploy/update.sh
```

Automatically, every ten minutes:

```bash
sudo cp deploy/caregraph-update.{service,timer} /etc/systemd/system/
sudo systemctl enable --now caregraph-update.timer
journalctl -u caregraph-update -f
```

In-flight requests drain for up to 20 seconds; `stop_grace_period` is 30, so
Docker does not interrupt it.

To pin or roll back, name a build in `.env` and re-run the script:

```bash
CAREGRAPH_TAG=sha-1a2b3c4
```

Without a registry — no network to GHCR, or a fork — the server can still build
for itself:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --profile app --profile edge up -d --build
```

## What this deliberately does not do

- **No push-based deployment.** GitHub Actions never connects to the server.
  That would mean an SSH key for the machine held as a repository secret, and SSH
  reachable from GitHub's runner ranges — which are broad enough that a firewall
  cannot meaningfully narrow them. A compromised workflow would then be a shell
  here. Pulling costs at most ten minutes of delay.
- **No secret manager.** `.env` at mode 0600 on one host. A managed secret store
  earns its complexity with several hosts, not with one.
- **No monitoring beyond `/readyz`.** The container healthcheck restarts nothing
  by itself; it reports. Alerting is E4-S3.

## If you announce this publicly

Running a reachable endpoint is not the same as publishing the code, and the
obligations attach to the first: an Impressum with a summonable address, and a
privacy notice — the API logs client IP addresses, which key the rate limiter and
the failed-authentication budget. An unannounced instance behind an API key is a
test environment; a published URL is a service.
