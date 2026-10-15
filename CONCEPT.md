# API Deprecation Watchdog

## Goal

API Deprecation Watchdog is a lightweight, self-hosted service that watches the
official changelogs, deprecation feeds and status pages of the external APIs and
SDKs a team depends on. It extracts the changes that actually matter, scores them
by how likely they are to break an existing integration, stores a full history,
and pushes prioritized warnings into the channels developers already use
(Slack, e-mail, and the built-in dashboard).

The product exists because deprecations and breaking changes are usually
announced in prose — buried in a changelog, a blog post or a dashboard banner —
long before the breaking change ships. Teams find out too late, which causes
unplanned outages and emergency migrations. The Watchdog turns that scattered,
heterogeneous noise into a single prioritized alert stream.

## Intended users

- **Platform / integration teams** in B2B SaaS companies that depend on several
  third-party APIs and SDKs and have no central deprecation monitoring.
- **On-call engineers** who want an early, low-noise signal before a dependency
  breaks in production.
- **Engineering leads** who need an auditable history of what changed, when it
  was announced, and whether the team has been warned.

## Value proposition

- Deprecation-focused detection instead of generic uptime/API monitoring.
- Rule-based extraction from heterogeneous sources (RSS, HTML, JSON,
  statuspage.io), with per-source selectors for precision.
- Relevance filtering against the API versions a team actually uses, so the
  signal stays small.
- Delivery into existing developer workflows; no agent installation required.

## Current capabilities (MVP increment)

- **Source registry:** add, list and remove monitored sources with a kind
  (`rss`, `html`, `json`, `statuspage`), an API name, the tracked versions and an
  optional per-source selector.
- **Parsers:** built-in parsers for RSS/Atom XML, JSON changelogs (several
  common shapes), HTML changelogs (headings + following content) and
  statuspage.io JSON (`incidents` + `scheduled_maintenances`), all
  dependency-free.
- **Per-source selectors:** `html` sources accept a CSS-like simple selector
  (tag, `#id`, `.class`, e.g. `h2.deprecation` or `#changelog`) and `json`
  sources accept a dotted path (e.g. `data.releases`), so noisy pages yield the
  right blocks without code changes.
- **Relevance engine:** detects deprecation / breaking-change language
  (`deprecated`, `sunset`, `end of life`, `removed`, `breaking`, …) and boosts
  entries that mention a tracked version. Classifies each hit as
  `critical`, `warning` or `info`.
- **Persistence:** SQLite store with de-duplication by content hash, so the same
  announcement is only alerted once.
- **Notifications:** Slack incoming webhook, SMTP e-mail and a console channel;
  channels are optional and enabled through configuration.
- **Web dashboard:** an accessible overview of sources, recent alerts and scan
  statistics, plus a JSON API.
- **Operations:** HTTP server on port `8080`, `GET /healthz`, background polling
  scheduler, optional bearer-token protection for mutating endpoints, non-root
  container, Helm chart, and a `linux/amd64` + `linux/arm64` image.

## HTTP API

| Method | Path              | Purpose                                   |
| ------ | ----------------- | ----------------------------------------- |
| GET    | `/healthz`        | Liveness/readiness probe                  |
| GET    | `/`               | HTML dashboard                            |
| GET    | `/api/sources`    | List monitored sources                    |
| POST   | `/api/sources`    | Register a source (token protected)       |
| DELETE | `/api/sources/:id`| Remove a source (token protected)         |
| GET    | `/api/alerts`     | List alerts, newest first                 |
| GET    | `/api/entries`    | List parsed changelog entries             |
| POST   | `/api/check`      | Run a scan now (token protected)          |
| GET    | `/api/stats`      | Aggregate counters                        |

## Selector syntax

The optional `selector` field on a source narrows extraction without any code
change. It means different things per kind:

- **`html`** – a CSS-like simple selector: an optional tag name, an optional
  `#id`, and an optional `.class` (or combination). A block matches when the
  block itself or one of its ancestors satisfies the selector, so `#id` scopes
  extraction to a container. When the selector contains no tag/`#id`/`.class`
  token it keeps the legacy behaviour of filtering blocks by text substring.
  - `h2.deprecation` → only `<h2 class="deprecation">` blocks.
  - `#changelog` → only the blocks nested inside `id="changelog"`.
- **`json`** – a dotted path into the document.
  - `data.releases` → walk `data` then `releases` before collecting entries.

Example source registrations:

```sh
# HTML changelog: only grab release headings marked as deprecations.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Stripe","url":"https://docs.stripe.com/changelog","kind":"html",
       "api_name":"Stripe","selector":"h2.deprecation","tracked_versions":["2024-01-01"]}'

# JSON changelog: only read the releases array.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme","url":"https://api.acme.com/changelog.json","kind":"json",
       "api_name":"Acme","selector":"data.releases","tracked_versions":["v2"]}'
```

## Next direction

1. **More source adapters:** GitHub releases and OpenAPI diff detection.
2. **Smarter relevance:** semantic matching of version ranges (semver / date
   versions) and per-team severity rules.
3. **Integrations:** GitHub/GitLab issues and Jira as delivery targets, plus
   digest notifications.
4. **Multi-tenant onboarding:** organizations, API keys and shared source
   templates for the manual concierge phase.
5. **Metrics export:** Prometheus endpoint for adoption and reliability
   monitoring.

## Architecture

```
sources ──▶ fetcher ──▶ parsers ──▶ relevance ──▶ store (SQLite)
                                              └─▶ notifiers (Slack / e-mail / console)
                                                        ▲
HTTP API + dashboard ──▶ engine (scan) ──▶ scheduler (background poll)
```

The design keeps a hard separation between I/O (`fetcher`), pure logic
(`parsers`, `relevance`), state (`store`), side effects (`notify`) and the
wiring (`engine`, `app`). This keeps the core testable offline with a fake
fetcher and no network access.

## Constraints and risks

- Depends on the structure and availability of external changelog sources;
  parsing is heuristic and can degrade when a source changes layout.
- Source formats vary widely, which is why per-source kinds and selectors
  exist.
- Teams may ignore deprecation warnings; prioritization and low noise are the
  mitigation.
- Integrating with private repositories or trackers raises access/privacy
  questions; the MVP deliberately keeps credentials in the deployment
  environment only.
