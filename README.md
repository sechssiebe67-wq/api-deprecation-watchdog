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

- **Source registry** for RSS/Atom, JSON, HTML, statuspage.io, GitHub releases
  and OpenAPI/Swagger specs (`rss`, `json`, `html`, `statuspage`, `github`,
  `openapi`).
- **Parsers** for all six formats, with no third-party dependencies. The
  `github` kind accepts either `https://api.github.com/repos/<owner>/<repo>/releases`
  or the repository HTML URL `https://github.com/<owner>/<repo>`, which is
  normalized to the API URL on registration.
- **OpenAPI diff detection**: the `openapi` kind diffs two spec versions (from
  a selected list) and emits one entry per structural change — removed paths or
  operations, narrowed enums, added required parameters — with a severity
  derived from the kind of change rather than from changelog prose.
- **Per-source selectors** for precise extraction: a CSS-like selector for
  `html` (`h2.deprecation`, `#changelog`) and a dotted path for `json`,
  `github` and `openapi` (`data.releases`, `versions`).
- **Relevance engine** that detects deprecation / breaking-change language and
  boosts entries referencing tracked versions; classifies hits as `critical`,
  `warning` or `info` (the severity ladder, highest to lowest).
- **Per-source severity floor**: each source has a `min_severity` (default
  `info`) that suppresses alerts below the chosen level while still recording
  the entries.
- **SQLite history** with content-hash de-duplication (no repeated alerts).
- **Notifications** via Slack webhook, SMTP e-mail and console.
- **Digest notifications** that batch alerts into a single grouped, severity-
  ordered summary on a configurable cadence (`off`, `digest` or `both`).
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
| `DIGEST_MODE`            | `off`   | `off`, `digest` or `both` (see below)               |
| `DIGEST_INTERVAL_SECONDS`| `86400` | Digest flush cadence (minimum `300`)                |
| `DIGEST_MAX_ALERTS`      | `50`    | Maximum alerts rendered per digest message          |
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
| GET    | `/api/digests`     | List digests, newest first (`?limit=`)    |
| GET    | `/api/digests/:id` | Fetch one digest (`404` when unknown)     |
| GET    | `/api/stats`       | Aggregate counters                        |
| POST   | `/api/check`       | Run a scan now (token protected)          |
| POST   | `/api/digest/flush`| Build and deliver a digest (token protected) |

Register a source. The `POST /api/sources` payload accepts:

| Field              | Required | Purpose                                                       |
| ------------------ | -------- | ------------------------------------------------------------- |
| `name`             | yes      | Display name                                                  |
| `url`              | yes      | `http(s)` URL to fetch                                         |
| `kind`             | yes      | `rss`, `json`, `html`, `statuspage`, `github` or `openapi`   |
| `api_name`         | no       | API/SDK the source documents                                  |
| `tracked_versions` | no       | Versions to boost during relevance scoring                    |
| `selector`         | no       | Extraction hint; CSS-like for `html`, dotted path for `json`/`github`/`openapi` |
| `enabled`          | no       | Whether the scheduler polls it (default `true`)               |
| `min_severity`     | no       | Lowest severity that creates an alert: `critical`, `warning` or `info` (default `info`) |

For `html`, `selector` is a CSS-like simple selector (optional tag, `#id`,
`.class`, or a combination). It matches a block when the block or an ancestor
satisfies it, so `#changelog` scopes extraction to that container. For `json`,
`github` and `openapi`, `selector` is a dotted path such as `data.releases` or
`versions`.

For `github` sources, `url` may be either the releases REST API URL
(`https://api.github.com/repos/<owner>/<repo>/releases`) or the repository HTML
URL (`https://github.com/<owner>/<repo>`, with or without a trailing `.git`).
The HTML form is normalized to the API URL when the source is stored. Each
release becomes an entry whose title is the release `name` or `tag_name`, body
is the release `body`, link is `html_url`, date is `published_at`, and whose
categories include the `tag_name` and, when set, `prerelease`; draft releases
are skipped.

For `openapi` sources, `url` points at an OpenAPI or Swagger JSON document.
Without a `selector` the document itself is treated as a single spec; with a
dotted `selector` (for example `versions`) it is walked to a list of spec
versions, and each consecutive pair is diffed oldest first. A single spec is
diffed against the empty spec, so the first registration surfaces the whole
surface as `info` entries instead of staying silent. Every structural change
becomes one entry titled `"<METHOD> <path>: <change kind>"`, with the old and
new value in the body, the change kind and affected path in `categories`, a
link built from `url` plus the spec's `info.contact.url`, and `published` from
the newer spec's `info.version`. The adapter is dependency-free and relies only
on the standard library.

The severity is derived from the kind of change, not from keywords:

| Severity  | Change kinds                                                                 |
| --------- | ---------------------------------------------------------------------------- |
| `critical`| removed path, removed operation, removed required parameter, removed enum value, changed parameter location, changed response status code set |
| `warning` | added required parameter, narrowed enum, changed parameter type, changed request/response schema type, changed `required` on a body property |
| `info`    | added path, added optional parameter, added enum value, added response status code, changed description |

Ordering-only changes and whitespace-only description changes are ignored, so
re-scanning an unchanged spec creates no new entries or alerts.

`min_severity` controls the per-source alert floor. The relevance engine still
classifies and stores every matched entry, but only entries whose severity ranks
at or above the floor become alerts. The ladder is `critical` > `warning` >
`info`, so a source with `min_severity: "warning"` ignores `info`-level release
notes but still alerts on `warning` and `critical` deprecations. Entries below
the floor remain in the history (`/api/entries`) and are de-duplicated normally.

```sh
# HTML changelog, narrowed to deprecation headings.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Stripe","url":"https://docs.stripe.com/changelog",
       "kind":"html","api_name":"Stripe","selector":"h2.deprecation",
       "tracked_versions":["2024-01-01"]}'

# A chatty source that only shows info noise: only alert on warning+.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Changelog","url":"https://api.acme.com/changelog.json",
       "kind":"json","api_name":"Acme","selector":"data.releases",
       "tracked_versions":["v2"],"min_severity":"warning"}'

# statuspage.io status feed.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Status","url":"https://status.acme.com/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Acme","tracked_versions":["v1"]}'

# GitHub releases: paste the repo URL, it is normalized to the REST API URL.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Widget SDK","url":"https://github.com/acme/widget","kind":"github",
       "api_name":"Widget","tracked_versions":["v2"],"min_severity":"warning"}'

# OpenAPI spec: diff the versions array and alert on warning+ structural changes.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme API spec","url":"https://api.acme.com/openapi.json",
       "kind":"openapi","api_name":"Acme","tracked_versions":["v2"],
       "selector":"versions","min_severity":"warning"}'
```

## Digest notifications

By default (`DIGEST_MODE=off`) every scan that finds new relevant entries sends
one notification per batch. On a busy day that is a stream of small messages.
Digest mode trades a little latency for a lot less noise: alerts are buffered
and delivered as a single summary, grouped by source and ordered by severity
(`critical` > `warning` > `info`), with newest alerts first. Each digest renders
at most `DIGEST_MAX_ALERTS` alerts and states how many were omitted.

| `DIGEST_MODE` | Behaviour                                                            |
| ------------- | ------------------------------------------------------------------- |
| `off`         | Immediate notification per scan (legacy default). No digest rows.   |
| `digest`      | No immediate message; alerts wait for the next digest flush.        |
| `both`        | Immediate message *and* the alerts are included in the next digest. |

Flushes happen on their own timer (`DIGEST_INTERVAL_SECONDS`, default daily) and
can be triggered on demand. A notifier failure is logged and leaves the digest
undelivered without aborting the request or the scheduler; because alerts are
only claimed by a digest once it has been delivered, a failed flush keeps them
pending so the next flush retries them.

```sh
# Deliver a digest now; returns the created digest, or {"digest": null}.
curl -X POST http://localhost:8080/api/digest/flush \
  -H "Authorization: Bearer $API_TOKEN"

# Browse delivered digests.
curl http://localhost:8080/api/digests
curl http://localhost:8080/api/digests/1
```

## Deployment

The Helm chart lives at `deploy/helm/api-deprecation-watchdog`. It renders the
image as `image.repository@image.digest` for immutable releases, supports
ingress through `ingress.enabled`, `ingress.host` and `ingress.className`,
runs as a non-root user with a read-only root filesystem, and persists SQLite
data on a PVC. Digest mode is configured through `config.digestMode`,
`config.digestIntervalSeconds` and `config.digestMaxAlerts` in `values.yaml`.

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
