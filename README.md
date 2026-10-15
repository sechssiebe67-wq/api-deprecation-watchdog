# API Deprecation Watchdog

A lightweight, dependency-free service that watches the changelogs, deprecation
feeds and status pages of the external APIs and SDKs your team depends on. It
extracts the changes that matter, ranks them by how likely they are to break an
integration, keeps a full history and pushes prioritized alerts to Slack, e-mail
or the built-in dashboard.

See [CONCEPT.md](CONCEPT.md) for the goal, the target users and the roadmap.

## Why

Deprecations and breaking changes are usually announced in prose — buried in a
changelog, a blog post or a dashboard banner — long before they ship. Teams find
out too late and end up firefighting. The Watchdog turns that scattered noise
into one prioritized alert stream, filtered against the API versions you
actually use.

## Features

- **Source registry** for RSS/Atom, JSON, HTML and statuspage.io (`rss`,
  `json`, `html`, `statuspage`).
- **Parsers** for all four formats, with no third-party dependencies.
- **Per-source selectors** for precise extraction: a CSS-like selector for
  `html` (`h2.deprecation`, `#changelog`) and a dotted path for `json`
  (`data.releases`).
- **Relevance engine** that detects deprecation / breaking-change language and
  boosts entries referencing tracked versions; classifies hits as `critical`,
  `warning` or `info`.
- **SQLite history** with content-hash de-duplication (no repeated alerts).
- **Notifications** via Slack webhook, SMTP e-mail and console.
- **Web dashboard** and a JSON API.
- **Operations-ready**: `GET /healthz`, background poller, non-root container,
  Helm chart, `linux/amd64` + `linux/arm64` image.

## Quick start

Run locally (no dependencies beyond Python 3.8+):

```sh
PORT=8080 DATA_DIR=./data python3 -m apiwatch
```

Then open <http://localhost:8080>. A fresh deployment seeds a few public
changelog sources; disable that with `SEED_SOURCES=false`.

Build and run the container:

```sh
docker build -t api-deprecation-watchdog .
docker run --rm -p 8080:8080 -v "$PWD/data:/data" api-deprecation-watchdog
```

## Configuration

All configuration is environment based:

| Variable                 | Default | Purpose                                            |
| ------------------------ | ------- | -------------------------------------------------- |
| `PORT`                   | `8080`  | HTTP port                                           |
| `HOST`                   | `0.0.0.0` | HTTP bind address                                 |
| `DATA_DIR`               | `/data` | Directory for `watchdog.db` (SQLite)                |
| `POLL_INTERVAL_SECONDS`  | `3600`  | Background scan interval                            |
| `FETCH_TIMEOUT_SECONDS`  | `20`    | Per-source HTTP timeout                             |
| `DISABLE_SCHEDULER`      | `false` | Disable background polling                          |
| `SEED_SOURCES`           | `true`  | Seed default sources on first start                 |
| `API_TOKEN`              | –       | Bearer token required for mutating endpoints        |
| `SLACK_WEBHOOK_URL`      | –       | Slack incoming webhook for alert delivery           |
| `SMTP_HOST` / `SMTP_PORT`| – / `587` | SMTP server for e-mail alerts                     |
| `SMTP_USER` / `SMTP_PASSWORD` | –  | SMTP credentials                                    |
| `SMTP_FROM` / `SMTP_TO`  | –       | Sender and comma-separated recipients               |
| `SMTP_STARTTLS`          | `true`  | Use STARTTLS                                        |
| `LOG_LEVEL`              | `INFO`  | Log verbosity                                       |

## HTTP API

| Method | Path               | Purpose                                   |
| ------ | ------------------ | ----------------------------------------- |
| GET    | `/healthz`         | Liveness / readiness probe                |
| GET    | `/`                | HTML dashboard                            |
| GET    | `/api/sources`     | List monitored sources                    |
| POST   | `/api/sources`     | Register a source (token protected)       |
| GET    | `/api/sources/:id` | Fetch one source                          |
| DELETE | `/api/sources/:id` | Remove a source (token protected)         |
| GET    | `/api/alerts`      | List alerts (`?limit=`, `?severity=`)     |
| GET    | `/api/entries`     | List parsed entries (`?source_id=`)       |
| GET    | `/api/stats`       | Aggregate counters                        |
| POST   | `/api/check`       | Run a scan now (token protected)          |

Register a source. The `POST /api/sources` payload accepts:

| Field              | Required | Purpose                                                       |
| ------------------ | -------- | ------------------------------------------------------------- |
| `name`             | yes      | Display name                                                  |
| `url`              | yes      | `http(s)` URL to fetch                                         |
| `kind`             | yes      | `rss`, `json`, `html` or `statuspage`                         |
| `api_name`         | no       | API/SDK the source documents                                  |
| `tracked_versions` | no       | Versions to boost during relevance scoring                    |
| `selector`         | no       | Extraction hint; CSS-like for `html`, dotted path for `json`  |
| `enabled`          | no       | Whether the scheduler polls it (default `true`)               |

For `html`, `selector` is a CSS-like simple selector (optional tag, `#id`,
`.class`, or a combination). It matches a block when the block or an ancestor
satisfies it, so `#changelog` scopes extraction to that container. For `json`,
`selector` is a dotted path such as `data.releases`.

```sh
# HTML changelog, narrowed to deprecation headings.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Stripe","url":"https://docs.stripe.com/changelog",
       "kind":"html","api_name":"Stripe","selector":"h2.deprecation",
       "tracked_versions":["2024-01-01"]}'

# statuspage.io status feed.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Status","url":"https://status.acme.com/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Acme","tracked_versions":["v1"]}'
```

## Deployment

The Helm chart lives at `deploy/helm/api-deprecation-watchdog`. It renders the
image as `image.repository@image.digest` for immutable releases, supports
ingress through `ingress.enabled`, `ingress.host` and `ingress.className`,
runs as a non-root user with a read-only root filesystem, and persists SQLite
data on a PVC.

```sh
helm install watchdog deploy/helm/api-deprecation-watchdog \
  --set image.repository=ghcr.io/your-org/api-deprecation-watchdog \
  --set image.digest=sha256:... \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=watchdog.example.com \
  --set existingSecret=watchdog-secrets
```

`existingSecret` should provide `API_TOKEN`, `SLACK_WEBHOOK_URL` and `SMTP_*`
keys. `.github/workflows/factory-release.yml` builds and publishes the multi-arch
image and the immutable release manifest.

## Development

The runtime uses only the Python standard library. Run the product tests:

```sh
python3 -m unittest discover -s tests -v
```

Run the pipeline tests:

```sh
python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v
```
