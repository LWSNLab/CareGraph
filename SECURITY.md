# Security Policy

CareGraph is public-interest infrastructure for German health and care data. We
take security reports seriously and will work with you in good faith.

## Reporting a Vulnerability

**Please do not open a public issue for security problems.**

Report privately via **[GitHub Security Advisories](https://github.com/LWSNLab/CareGraph/security/advisories/new)**
("Report a vulnerability"). This keeps the discussion private until a fix exists.

If that is unavailable to you, contact the maintainers through the address
listed on the [organisation profile](https://github.com/LWSNLab) and mention
"CareGraph security" in the subject.

### What to include

- The affected component (Go API gateway, Python pipelines, database schema, CI).
- Version, commit SHA or branch.
- Steps to reproduce, ideally minimal.
- The impact you believe it has.

You do not need a polished write-up — a clear reproduction is worth more than a
formal report.

### What to expect

| Stage | Target |
| :-- | :-- |
| Acknowledgement of your report | within 3 working days |
| Initial assessment and severity | within 7 working days |
| Fix or documented mitigation | depends on severity; we keep you updated |

We follow **coordinated disclosure**: we ask that you give us reasonable time to
ship a fix before publishing. We will credit you in the advisory unless you
prefer to stay anonymous.

This is a small project without a bug-bounty programme — we cannot offer
monetary rewards, and we would rather say so plainly than imply otherwise.

## Scope

**In scope**

- The Go API gateway (`cmd/`, `internal/`) — authentication, rate limiting, injection, data exposure.
- The Python ingestion pipelines (`pipelines/`).
- Database schema and migrations (`db/`) — including row-level security assumptions.
- CI/CD workflows and supply-chain configuration (`.github/`).

**Out of scope**

- Vulnerabilities in third-party services we merely consume (OpenStreetMap/Overpass, GovData) — please report those to their maintainers.
- Findings that require a compromised maintainer account or physical access.
- Missing hardening that has no demonstrable impact (e.g. a header on a static page).
- Automated scanner output without a working reproduction.

## Data and Privacy

CareGraph processes **publicly listed institution data** (care providers,
statutory insurers). It holds **no patient data and no personal health records**.

Two areas nevertheless deserve careful reports:

- **Location privacy.** Radius queries can expose a user's whereabouts. The
  gateway must not log exact coordinates; see
  [Security & Privacy](https://github.com/LWSNLab/CareGraph_Doc/blob/main/docs/architecture/security.md).
  A leak of precise coordinates into logs or responses **is** a valid finding.
- **Sole traders.** Some care providers are natural persons, so their published
  contact data can be personal data under the GDPR. Reports about improper
  handling of such records are in scope.

## Security Measures in Place

| Area | Measure |
| :-- | :-- |
| Static analysis | CodeQL (`security-extended` queries), Go `vet` |
| Dependencies | Dependabot alerts and grouped update PRs; `govulncheck` (Go), `pip-audit` (Python) |
| Secrets | `gitleaks` over the full history on every push and pull request |
| Database | Least-privilege roles — write-scoped ingestion, read-only gateway; row-level security on managed Postgres |
| API | API keys stored as Argon2id hashes, never plaintext; Redis-backed rate limiting; TLS 1.3 |
| CI | Least-privilege `GITHUB_TOKEN` permissions, pinned runtime versions |

## Supported Versions

CareGraph is pre-1.0 and under active development. Security fixes are applied to
the default branch (`develop`) and to `main`. There are no maintained release
branches yet; this section will be updated at the 1.0 release.
