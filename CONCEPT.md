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
  statuspage.io, GitHub and GitLab releases and OpenAPI/Swagger specs), with
  per-source selectors for precision, plus true schema diffing for OpenAPI.
- Relevance filtering against the API versions a team actually uses, so the
  signal stays small.
- Delivery into existing developer workflows; no agent installation required.

## Current capabilities (MVP increment)

- **Source registry:** add, list and remove monitored sources with a kind
  (`rss`, `html`, `json`, `statuspage`, `github`, `gitlab`, `openapi`), an API
  name, the tracked versions and an optional per-source selector.
- **Parsers:** built-in parsers for RSS/Atom XML, JSON changelogs (several
  common shapes), HTML changelogs (headings + following content),
  statuspage.io JSON (`incidents` + `scheduled_maintenances`), GitHub
  releases (`releases` + pre-releases via the REST API), GitLab releases
  (`releases` via the REST API) and OpenAPI/Swagger specs (structural
  diffing), all dependency-free.
  A `github` registration accepts either the API URL
  (`https://api.github.com/repos/owner/repo/releases`) or the repository HTML
  URL (`https://github.com/owner/repo`), which is normalized to the API URL;
  release `name`/`tag_name` becomes the title, `body` the body, `html_url` the
  link, `published_at` the date and `tag_name`/`prerelease` the categories,
  while drafts are skipped.
  A `gitlab` registration mirrors that contract: it accepts either the project
  HTML URL (`https://gitlab.com/group/project`, with an optional trailing
  `.git` or `/`, nested subgroups allowed) or the releases API URL
  (`https://gitlab.com/api/v4/projects/<id-or-path>/releases`), normalizing
  the HTML form to
  `https://gitlab.com/api/v4/projects/<group>%2F<project>/releases`; release
  `name`/`tag_name` becomes the title, `description` the body, `_links.self`
  the link, `released_at` the date and `tag_name`/`upcoming_release` the
  categories (GitLab has no drafts, so nothing is skipped).
- **OpenAPI diff detection:** an `openapi` source fetches a spec URL, walks an
  optional dotted `selector` to a list of spec versions (or treats the document
  itself as a single spec), diffs each consecutive pair and emits one entry per
  structural change instead of relying on prose. For a single spec the document
  is diffed against the empty spec, so first-time registration surfaces the
  whole surface as `info` entries — the baseline is visible without alerting.
  The severity is derived from the kind of change:
  - `critical` — removed path, removed operation, removed required parameter,
    removed enum value, changed parameter location, changed response status code
    set;
  - `warning` — added required parameter, narrowed enum, changed parameter type,
    changed request/response schema type, changed `required` on a body property;
  - `info` — added path, added optional parameter, added enum value, added
    response status code, changed description.
  Ordering-only changes and whitespace-only description changes are ignored so
  the content-hash de-duplication in `store.py` keeps working. Each entry's
  title is `"<METHOD> <path>: <change kind>"`, its body carries the old and new
  value, its link is `base_url` plus the spec's `info.contact.url` when present,
  its `published` date comes from the newer spec's `info.version`, and its
  categories include the change kind and the affected path.
- **Per-source selectors:** `html` sources accept a CSS-like simple selector
  (tag, `#id`, `.class`, e.g. `h2.deprecation` or `#changelog`) and `json` /
  `github` / `gitlab` / `openapi` sources accept a dotted path (e.g.
  `data.releases` or `versions`), so noisy pages yield the right blocks without
  code changes.
- **Relevance engine:** detects deprecation / breaking-change language
  (`deprecated`, `sunset`, `end of life`, `removed`, `breaking`, …) and boosts
  entries that mention a tracked version. Classifies each hit as
  `critical`, `warning` or `info`, a ladder where `critical` ranks highest and
  `info` lowest.
- **Per-source severity floor:** every source carries a `min_severity`
  (`critical`, `warning` or `info`, default `info`). The engine classifies and
  stores every matched entry regardless, but only creates an alert when the
  entry's severity ranks at or above the source's floor. This lets operators
  mute a chatty source that only ever emits `info`-level release notes while a
  source documenting a critical dependency keeps surfacing everything. Entries
  below the floor are still persisted, so history, `/api/entries` and
  content-hash de-duplication are unaffected.
- **Per-source scan intervals:** every source optionally carries a
  `scan_interval_seconds` (a positive integer, default `None`). When set, the
  background scheduler polls that source on its own cadence; when omitted the
  source falls back to the deployment-wide `POLL_INTERVAL_SECONDS`, so existing
  sources and databases are unchanged. The scheduler tracks the last scan time
  per source and only scans a source once its effective interval has elapsed,
  waking up at the smallest effective interval among enabled sources so a
  fast-moving dependency is not held back by a slow global timer. Both the
  per-source value and the global default are floored at 30 seconds to avoid
  hammering a source. A manual `POST /api/check` (and `Engine.scan_all()`)
  deliberately ignores the intervals and scans every enabled source.
- **Persistence:** SQLite store with de-duplication by content hash, so the same
  announcement is only alerted once. The source table stores
  `scan_interval_seconds`, and a lightweight migration adds the column to
  databases created before it existed, leaving old rows at `NULL`.
- **Per-source history API:** each source's recorded entries are browsable
  newest-first through `GET /api/sources/:id/entries` (optional `?limit=`,
  default `100`), so the stored history of one dependency can be inspected
  without filtering the global `/api/entries` feed. An unknown id returns `404`
  and a non-integer id or limit returns `400`; the `/entries` suffix leaves
  `GET` and `DELETE /api/sources/:id` unchanged.
- **Notifications:** Slack incoming webhook, SMTP e-mail and a console channel;
  channels are optional and enabled through configuration.
- **Per-source notification routing:** every source carries a `notify_channels`
  list naming the notifier channels it may target (`console`, `slack`, `email`)
  or the literal `all`. The default is `["all"]`, which preserves the original
  fan-out to every configured notifier, so existing sources and deployments are
  unchanged. A chatty `info`-only source can be routed to Slack only, while a
  critical dependency can be escalated to a dedicated e-mail channel; an empty
  list is treated as `all`. The same routing applies to digests: alerts are
  grouped by their recipient set and each notifier receives only the alerts
  routed to it, so a notifier with no matching alert is never invoked and a
  routed alert stays pending until a matching notifier accepts it.
- **Digest notifications:** an optional digest mode that buffers new alerts and
  delivers one grouped summary per digest window instead of one message per
  scan. Alerts are grouped by source, ordered by severity (critical > warning >
  info) and then by source name, with newest alerts first inside each group; the
  body is capped at `DIGEST_MAX_ALERTS` and states how many alerts were omitted.
  Three modes are available through `DIGEST_MODE`:
  - `off` (default) — immediate notification per scan batch, exactly the legacy
    behaviour; no digest rows are created.
  - `digest` — alerts are stored with `notified = 0` and no immediate message is
    sent; a digest flush delivers a single summary and then marks the alerts
    notified.
  - `both` — the immediate notification is sent *and* the same alerts remain
    eligible for the next digest.
  Flushes run on their own timer (`DIGEST_INTERVAL_SECONDS`, minimum `300`,
  default daily) or on demand via `POST /api/digest/flush`; a notifier failure
  is logged and leaves the digest undelivered without aborting the request or
  the scheduler. Alerts are only claimed by a digest after successful delivery,
  so a failed flush leaves them pending and the next flush retries them.
- **Web dashboard:** an accessible overview of sources, recent alerts, recent
  digests, pending-digest alerts and scan statistics, plus a JSON API.
- **Operations:** HTTP server on port `8080`, `GET /healthz`, background polling
  scheduler, optional bearer-token protection for mutating endpoints, non-root
  container, Helm chart, and a `linux/amd64` + `linux/arm64` image.

## HTTP API

| Method | Path              | Purpose                                   |
| ------ | ----------------- | ----------------------------------------- |
| GET    | `/healthz`        | Liveness/readiness probe                  |
| GET    | `/`               | HTML dashboard                            |
| GET    | `/api/sources`    | List monitored sources (incl. `min_severity`, `notify_channels`, `scan_interval_seconds`) |
| POST   | `/api/sources`    | Register a source, incl. optional `min_severity`, `notify_channels` and `scan_interval_seconds` (token protected) |
| GET    | `/api/sources/:id/entries` | List one source's entries, newest first (`?limit=`) |
| DELETE | `/api/sources/:id`| Remove a source (token protected)         |
| GET    | `/api/alerts`     | List alerts, newest first                 |
| GET    | `/api/entries`    | List parsed changelog entries             |
| GET    | `/api/digests`    | List digests, newest first (`?limit=`)    |
| GET    | `/api/digests/:id`| Fetch one digest (`404` when unknown)     |
| POST   | `/api/digest/flush`| Build and deliver a digest (token protected) |
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
- **`github`** – the same dotted path syntax, useful for wrapped API payloads.
  - `data.releases` → walk `data` then `releases` before collecting entries.
- **`gitlab`** – the same dotted path syntax, useful for wrapped API payloads.
  - `data.releases` → walk `data` then `releases` before collecting entries.
- **`openapi`** – the same dotted path syntax, pointing at the list of spec
  versions in the fetched document. Each consecutive pair is diffed oldest
  first. When the selector is omitted (or resolves to a single spec) the
  document is treated as one spec and diffed against the empty spec, so a
  first registration reports the whole surface as `info` entries.
  - `versions` → walk the top-level `versions` array of spec objects.

Every source also accepts an optional `min_severity` (`critical`, `warning` or
`info`, default `info`). An entry is only turned into an alert when its severity
ranks at or above this floor; lower-severity entries are still parsed and stored.

## Scan cadence

The optional `scan_interval_seconds` field on a source overrides the global
`POLL_INTERVAL_SECONDS` cadence for that dependency. It is a positive integer
(number of seconds) or omitted:

- omitted (or `null`) → the source keeps the global cadence, the original
  behaviour, so existing registrations and databases are unaffected;
- `900` → the scheduler polls that source every 15 minutes;
- `86400` → the scheduler polls that source once a day.

A value that is zero, negative, non-integer or non-numeric is rejected with a
`400` naming `scan_interval_seconds`. The background scheduler keeps an
in-memory last-scan timestamp per source and only scans a source when its
effective interval has elapsed; it wakes at the smallest effective interval so a
15-minute source is still honoured when `POLL_INTERVAL_SECONDS` is an hour. The
first tick after start scans every source. `POST /api/check` (and the engine's
`scan_all()`) always scans every enabled source regardless of the field, so a
manual check is never filtered by cadence.

## Routing

The optional `notify_channels` field on a source selects which notifier channels
receive its alerts. It is a list whose entries are `console`, `slack`, `email`
or the literal `all`:

- omitted or `all` (the default) → every configured notifier, the original
  fan-out behaviour;
- `["slack"]` → only the Slack notifier;
- `["email"]` → only the e-mail notifier;
- `["console", "slack"]` → the console and Slack notifiers;
- `[]` (empty list) → treated as `all`.

Unknown entries are rejected with a `400` that names the allowed values. The
deployment-wide `NOTIFY_CHANNELS` environment variable supplies the default for
sources that omit the field, so an operator can change the default without
editing every source. The engine honours the field for both immediate dispatch
and digest delivery: a notifier is only invoked when at least one alert is routed
to it, and an alert is only marked notified once a routed notifier has accepted
it, so routing never silently drops an alert.

Example source registrations:

```sh
# HTML changelog: only grab release headings marked as deprecations.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Stripe","url":"https://docs.stripe.com/changelog","kind":"html",
       "api_name":"Stripe","selector":"h2.deprecation","tracked_versions":["2024-01-01"]}'

# JSON changelog: only read the releases array, and skip info-level noise.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme","url":"https://api.acme.com/changelog.json","kind":"json",
       "api_name":"Acme","selector":"data.releases","tracked_versions":["v2"],
       "min_severity":"warning"}'

# GitHub releases: paste the repo URL, it is normalized to the REST API URL.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Widget SDK","url":"https://github.com/acme/widget","kind":"github",
       "api_name":"Widget","tracked_versions":["v2"],"min_severity":"warning"}'

# GitLab releases: paste the project URL, it is normalized to the releases API.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Widget SDK (GitLab)","url":"https://gitlab.com/acme/widget",
       "kind":"gitlab","api_name":"Widget","tracked_versions":["v2"],
       "min_severity":"warning"}'

# OpenAPI spec: diff the versions array and only alert on warning or critical
# structural changes (removed paths, narrowed enums, added required params, ...).
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme API spec","url":"https://api.acme.com/openapi.json",
       "kind":"openapi","api_name":"Acme","tracked_versions":["v2"],
       "selector":"versions","min_severity":"warning"}'

# Route a chatty source to Slack only; the e-mail on-call inbox stays quiet.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Release Notes","url":"https://api.acme.com/changelog.json",
       "kind":"json","api_name":"Acme","selector":"data.releases",
       "notify_channels":["slack"]}'

# Volatile dependency: poll every 15 minutes instead of the global hour.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Payments API","url":"https://status.payments.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Payments","tracked_versions":["v1"],
       "min_severity":"warning","scan_interval_seconds":900}'
```

## Next direction

*Shipped:* **digest notifications** (see *Current capabilities*) — grouped,
severity-ordered summaries delivered on a configurable cadence in addition to
immediate alerts.

*Shipped:* **per-source severity floor** (see *Current capabilities*) — a
`min_severity` on each source that suppresses alerts below the chosen severity
while still recording the underlying entries.

*Shipped:* **GitHub releases adapter** (see *Current capabilities*) — a
`github` source kind that watches a repository's releases and pre-releases
through the GitHub REST API and normalizes the repository HTML URL to the API
URL, so breaking-change notes in release bodies are parsed and classified.

*Shipped:* **GitLab releases adapter** (see *Current capabilities*) — a
`gitlab` source kind that mirrors the GitHub adapter against the public GitLab
REST API, normalizing the project HTML URL to the releases API URL, so teams
whose dependencies are hosted on GitLab get the same release-body signal.

*Shipped:* **OpenAPI diff detection** (see *Current capabilities*) — an
`openapi` source kind that diffs two spec versions (or one spec against the
empty spec) and emits one entry per structural change, with the severity
derived from the kind of change rather than from prose.

*Shipped:* **per-source notification routing** (see *Current capabilities*) — a
`notify_channels` list on each source that targets a subset of the configured
notifier channels, so the *Integrations* / delivery-target and *Smarter
relevance* items below are now partially shipped: the existing channels are
usable per dependency without adding a new external integration.

*Shipped:* **per-source history API** (see *Current capabilities*) —
`GET /api/sources/:id/entries` exposes one dependency's stored entry history
newest-first, a first step toward richer per-dependency history views.

*Shipped:* **per-source scan intervals** (see *Current capabilities*) — a
`scan_interval_seconds` override that lets a volatile dependency be polled on a
fast cadence while a stable SDK keeps the global `POLL_INTERVAL_SECONDS`,
without changing the manual `POST /api/check` semantics.

1. **More source adapters:** additional structured feeds beyond the shipped
   GitHub and GitLab releases and OpenAPI adapters. *Partially shipped:*
   the `gitlab` adapter now covers GitLab-hosted releases; further forges and
   trackers remain.
2. **Smarter relevance:** semantic matching of version ranges (semver / date
   versions), per-team severity rules and richer keyword tuning. *Partially
   shipped:* per-source `min_severity` and `notify_channels` already let a team
   tune the volume and destination of alerts per dependency.
3. **Integrations:** GitHub/GitLab issues and Jira as delivery targets.
   *Partially shipped:* per-source `notify_channels` routes alerts to the
   existing Slack, e-mail and console channels; tracker integrations remain to
   be built.
4. **Multi-tenant onboarding:** organizations, API keys and shared source
   templates for the manual concierge phase.
5. **Metrics export:** Prometheus endpoint for adoption and reliability
   monitoring.

## Architecture

```
sources ──▶ fetcher ──▶ parsers ──▶ relevance ──▶ store (SQLite)
                          │                     └─▶ notifiers (Slack / e-mail / console)
                          └─ adapters: rss · html · json · statuspage · github · gitlab · openapi
                                                        ▲
HTTP API + dashboard ──▶ engine (scan) ──▶ scheduler (background poll)
```

The `parsers` stage hosts one adapter per source kind; the `openapi` adapter is
a pure in-memory diff over two fetched spec versions and fits the same
`parse` → `ParsedEntry` contract as the text adapters.

The design keeps a hard separation between I/O (`fetcher`), pure logic
(`parsers`, `relevance`), state (`store`), side effects (`notify`) and the
wiring (`engine`, `app`). This keeps the core testable offline with a fake
fetcher and no network access.

The `scheduler` sits on top of `engine`: it keeps an in-memory last-scan time
per source, computes each source's effective interval (`scan_interval_seconds`
or `POLL_INTERVAL_SECONDS`) and calls `engine.scan_sources` only for the sources
that are due. `engine.scan_all` remains the "scan everything now" primitive used
by the manual check endpoint, so the two entry points share one scanning
implementation while keeping different cadence semantics.

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
