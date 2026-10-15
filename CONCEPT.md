# API Deprecation Watchdog

## Goal

API Deprecation Watchdog is a lightweight, self-hosted service that watches the
official changelogs, deprecation feeds and status pages of the external APIs and
SDKs a team depends on. It extracts the changes that actually matter, scores them
by how likely they are to break an existing integration, stores a full history,
and pushes prioritized warnings into the channels developers already use
(Slack, e-mail, an outbound JSON webhook, and the built-in dashboard).

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
  name, the tracked versions and an optional per-source selector. Each source
  may also carry custom HTTP `headers` and a convenience `auth`
  (`"user:password"`) so authenticated and otherwise gated feeds can be
  monitored without forking the service, plus optional `extra_keywords` and
  `suppress_keywords` lists that extend or mute the relevance vocabulary for
  that dependency.
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
- **Per-source keyword overrides:** every source optionally carries
  `extra_keywords` and `suppress_keywords`, two lists of strings that tune the
  relevance vocabulary for that dependency alone. An `extra_keywords` term — for
  example a vendor that writes `"end of support"` instead of `"end of life"` —
  is matched case-insensitively on the same word boundaries as the built-in
  terms and raises an otherwise low-signal entry to `critical`, while a
  `suppress_keywords` term is removed from the matched set before
  classification, so a source can mute a noisy phrase without losing the rest
  of the vocabulary. Both lists default to `[]`, in which case the engine
  behaves exactly as before the feature existed; terms are trimmed and
  lower-cased on registration and stored JSON-encoded in the `sources` table.
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
- **Per-source scan jitter:** every source optionally carries a
  `scan_jitter_seconds` (a non-negative integer, default `0` / `None`, meaning
  no jitter). When set, the scheduler delays that source's next scan by a
  deterministic offset in `[0, scan_jitter_seconds]` derived from the source id
  (`id % (jitter + 1)`), so many sources that share the same effective interval
  no longer fire on the same tick. This spreads polling of a large registry
  across a window — reducing thundering-herd pressure on shared upstreams such
  as statuspages, GitHub/GitLab and vendor changelogs — while staying fully
  reproducible and testable: the same source id and jitter always yield the same
  offset, with no randomness. The effective next due time is
  `last_scan + effective_interval + offset`, where the effective interval is the
  per-source `scan_interval_seconds` when set and the deployment-wide
  `POLL_INTERVAL_SECONDS` otherwise, so jitter composes with both. A value of
  `0` (or omitting the field, or sending `null`) is the default and is
  byte-for-byte equivalent to the pre-feature scheduler (same due times, same
  wake-ups). The scheduler wakes at the smallest effective due time among
  enabled, non-tripped sources and does not busy-loop while a jittered source is
  not yet due. A manual `POST /api/check` (and `Engine.scan_all()`) ignores
  jitter and intervals alike and scans every enabled source immediately, exactly
  as it ignores intervals today.
- **Per-source fetch size limits:** each fetch reads at most
  `FETCH_MAX_BYTES` (default `2000000`, i.e. 2 MB, floored at `1024`) bytes of a
  response body, and every source may override that with a positive `max_bytes`.
  When a body is clipped the engine records the truncation, logs a warning and
  still parses the partial body so a large feed degrades gracefully instead of
  being dropped or tripping the circuit breaker; the signal is surfaced as
  `sources_truncated` in `GET /api/stats` and as a `truncated` badge in the
  dashboard's `Health` column.
- **Per-source request headers and basic auth:** every source optionally carries
  a `headers` object (a JSON map of header name to value) and an `auth`
  convenience field (`"user:password"`). The fetcher merges the custom headers
  over its defaults (`User-Agent`, `Accept`) and, when `auth` is set, adds an
  `Authorization: Basic <base64>` header. This unlocks private GitHub Enterprise
  or GitLab self-managed instances, token-gated statuspage.io pages and internal
  changelogs behind HTTP Basic auth without forking the service. Header names are
  normalized case-insensitively to a canonical `Title-Case` form so
  `user-agent` and `User-Agent` cannot both be stored; the last write wins.
   A source with neither field behaves exactly as before. The dashboard shows a
   compact lock / header-count indicator and never renders a header value or the
   credentials.
- **Per-source failure circuit breaker:** every source tracks a
  `failure_count` (consecutive failed scans) and an optional `breaker_until`
  (ISO-8601 UTC). A failed fetch — or a malformed OpenAPI document — increments
  the counter; once it reaches `BREAKER_THRESHOLD` (default `5`) the source is
  quarantined and `breaker_until` is set to `now + BREAKER_COOLDOWN_SECONDS`
  (default `1800`, minimum `60`). While tripped, the background scheduler and
  `Engine.scan_all()` skip the source and report it as *skipped* rather than
  failed, so a slow or hostile feed cannot starve the poll loop or be retried on
  every tick. A single successful scan resets `failure_count` to `0` and clears
  `breaker_until`, so the source is scanned again on the next tick; operators
  can also clear it immediately through `POST /api/sources/:id/reset`. The two
  fields are returned by `GET /api/sources` and `GET /api/sources/:id` and
  summarised by `sources_tripped` in `GET /api/stats`.
- **Persistence:** SQLite store with de-duplication by content hash, so the same
  announcement is only alerted once.   The source table stores
  `scan_interval_seconds`, `scan_jitter_seconds` (integer or `NULL`, where
  `NULL`/`0` means no jitter), `max_bytes` (integer or `NULL`), `headers` (JSON
  text), `auth` (text),
  `extra_keywords` and `suppress_keywords` (JSON text),
  `failure_count` (integer, default `0`), `breaker_until` (text) and
  `truncated` (integer, default `0`), and a
  lightweight migration adds each column to databases created before it existed,
  leaving old rows at `NULL`/empty/`0` (read back as `{}` / `None` / `[]` /
  `0` / `None` / `false`).
- **Per-source history API:** each source's recorded entries are browsable
  newest-first through `GET /api/sources/:id/entries` (optional `?limit=`,
  default `100`), so the stored history of one dependency can be inspected
  without filtering the global `/api/entries` feed. An unknown id returns `404`
  and a non-integer id or limit returns `400`; the `/entries` suffix leaves
  `GET` and `DELETE /api/sources/:id` unchanged.
- **Notifications:** Slack incoming webhook, SMTP e-mail, a generic JSON
  webhook and a console channel; channels are optional and enabled through
  configuration. The `webhook` channel POSTs a dependency-free JSON payload to
  `WEBHOOK_URL`, so a team can integrate any internal system (PagerDuty,
  Opsgenie, Mattermost, custom incident tooling) without adding a new external
  dependency or hard-coded integration.
- **Per-source notification routing:** every source carries a `notify_channels`
  list naming the notifier channels it may target (`console`, `slack`, `email`,
  `webhook`) or the literal `all`. The default is `["all"]`, which preserves the
  original fan-out to every configured notifier, so existing sources and
  deployments are unchanged. A chatty `info`-only source can be routed to Slack
  only, while a critical dependency can be escalated to a dedicated e-mail or
  webhook endpoint; an empty list is treated as `all`. The same routing applies
  to digests: alerts are grouped by their recipient set and each notifier
  receives only the alerts routed to it, so a notifier with no matching alert is
  never invoked and a routed alert stays pending until a matching notifier
  accepts it.
- **Per-source webhook override:** a source may set `notify_webhook_url` to an
  `http(s)` URL that overrides the global `WEBHOOK_URL` for that source's
  alerts (and its digests). This lets a critical dependency escalate to a
  dedicated incident endpoint while a chatty source keeps the default target.
  The field is optional; when omitted the source uses the global URL, and old
  databases are migrated with the column left `NULL`.
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
- **Data export:** a token-free, read-only `GET /api/export` that returns a
  single JSON snapshot of the whole watchdog state — `generated_at`, `stats`,
  `sources`, `entries`, `alerts` and `digests` — for offline analysis, incident
  reports or feeding other tools without writing a custom client. An optional
  `?limit=` (default `1000`, maximum `10000`) caps entries, alerts and digests
  independently while sources are always returned in full. The dashboard header
  carries a `Download JSON` link pointing at the same endpoint.
- **Web dashboard:** an accessible overview of sources, recent alerts, recent
  digests, pending-digest alerts and scan statistics, plus a JSON API and a
  `Download JSON` link to the export endpoint.
- **Operations:** HTTP server on port `8080`, `GET /healthz`, background polling
  scheduler, optional bearer-token protection for mutating endpoints, non-root
  container, Helm chart, and a `linux/amd64` + `linux/arm64` image.

## HTTP API

| Method | Path              | Purpose                                   |
| ------ | ----------------- | ----------------------------------------- |
| GET    | `/healthz`        | Liveness/readiness probe                  |
| GET    | `/`               | HTML dashboard                            |
| GET    | `/api/sources`    | List monitored sources (incl. `min_severity`, `notify_channels`, `notify_webhook_url`, `scan_interval_seconds`, `scan_jitter_seconds`, `max_bytes`, `headers`, `auth`, `extra_keywords`, `suppress_keywords`, `failure_count`, `breaker_until`, `truncated`) |
| POST   | `/api/sources`    | Register a source, incl. optional `min_severity`, `notify_channels`, `notify_webhook_url`, `scan_interval_seconds`, `scan_jitter_seconds`, `max_bytes`, `headers`, `auth`, `extra_keywords` and `suppress_keywords` (token protected) |
| GET    | `/api/sources/:id`| Fetch one source (incl. `scan_jitter_seconds` and breaker fields) |
| GET    | `/api/sources/:id/entries` | List one source's entries, newest first (`?limit=`) |
| POST   | `/api/sources/:id/reset` | Clear one source's `failure_count` and `breaker_until` (token protected) |
| DELETE | `/api/sources/:id`| Remove a source (token protected)         |
| GET    | `/api/alerts`     | List alerts, newest first                 |
| GET    | `/api/entries`    | List parsed changelog entries             |
| GET    | `/api/digests`    | List digests, newest first (`?limit=`)    |
| GET    | `/api/digests/:id`| Fetch one digest (`404` when unknown)     |
| POST   | `/api/digest/flush`| Build and deliver a digest (token protected) |
| POST   | `/api/check`      | Run a scan now (token protected)          |
| GET    | `/api/stats`      | Aggregate counters (incl. `sources_tripped`) |
| GET    | `/api/export`     | Full JSON snapshot of sources, entries, alerts and digests (`?limit=`, default `1000`, max `10000`) |

## Data export

`GET /api/export` returns one read-only JSON document containing the whole
watchdog state so a team can snapshot it for offline analysis, incident reports
or feed it into other tools without writing a custom client. The endpoint is
token-free, consistent with the other read-only routes (`/api/sources`,
`/api/alerts`, `/api/entries`), and never mutates the store.

The payload is:

```json
{
  "generated_at": "2024-05-01T12:00:00+00:00",
  "stats": { "sources": 2, "entries": 10, "...": "..." },
  "sources": [ { "id": 1, "name": "Stripe", "...": "..." } ],
  "entries": [ { "id": 1, "title": "v1 deprecated", "...": "..." } ],
  "alerts": [ { "id": 1, "severity": "critical", "...": "..." } ],
  "digests": [ { "id": 1, "alert_count": 3, "...": "..." } ]
}
```

- `generated_at` is the snapshot time as an ISO-8601 UTC timestamp.
- `stats` is exactly the `store.stats()` payload also served by
  `GET /api/stats`, so the snapshot self-describes the counts even when a
  collection is truncated.
- `sources`, `entries`, `alerts` and `digests` are lists of the existing
  `to_dict()` forms, newest first where the store already orders them.

The optional `?limit=` query parameter caps `entries`, `alerts` and `digests`
*independently*. It defaults to `1000` and must be an integer between `1` and
`10000`; a non-integer or out-of-range value is rejected with
`400 {"error": "invalid limit"}`. `sources` are always returned in full, so the
snapshot never hides which dependencies are configured. The dashboard header has
a `Download JSON` link for one-click access.

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

## Keyword overrides

The optional `extra_keywords` and `suppress_keywords` lists on a source tune the
relevance vocabulary for that dependency only, so a vendor that says `"will be
retired"`, `"EOL"` or an internal term the built-ins do not know can be detected
without forking the service:

- `extra_keywords` — terms that extend the built-in critical vocabulary. Each
  term is matched case-insensitively on the same word boundaries as the built-in
  terms (so `retired` does not match inside `retirement`-style surprises), and a
  match raises an otherwise low-signal entry to `critical`.
- `suppress_keywords` — terms removed from the matched set *before*
  classification. A source whose entries always say `"notice"` can mute that
  word, and an entry whose only match was suppressed becomes non-relevant and
  produces no alert.

Both fields are optional lists of non-empty strings; omitted means `[]`. Values
are trimmed and lower-cased on registration, and a non-list value or a
non-string/blank entry is rejected with
`400 {"error": "'extra_keywords' must be a list of strings"}` (naming the
offending field). Both lists are stored JSON-encoded in the `sources` table and
returned by `GET /api/sources` and `GET /api/sources/:id`; old databases are
migrated automatically and read back as `[]`. With both lists empty the engine,
alerts and stored entries are identical to before the feature existed.

```sh
# Vendor that writes "end of support": promote it to critical, and mute "notice".
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Vendor SDK","url":"https://vendor.example/changelog.json","kind":"json",
       "api_name":"Vendor","tracked_versions":["v2"],
       "extra_keywords":["retired","eol","end of support"],
       "suppress_keywords":["notice"]}'
```

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
`scan_all()`) always scans every healthy enabled source regardless of the field
(sources with a tripped circuit breaker are skipped, see *Circuit breaker*), so
a manual check is never filtered by cadence.

The optional `scan_jitter_seconds` field composes with the cadence. It is a
non-negative integer (number of seconds) or omitted:

- omitted, `null` or `0` → no jitter; the source's due time is exactly
  `last_scan + effective_interval`, the original behaviour, so existing
  registrations and databases are unaffected (and the scheduler's due times and
  wake-ups are byte-for-byte identical to before the feature);
- `300` → the scheduler adds a deterministic offset in `[0, 300]` to that
  source's next scan, derived from the source id, so many sources with the same
  effective interval no longer fire on the same tick.

The offset is `source.id % (scan_jitter_seconds + 1)`: it is stable for a given
source and jitter, never random, so scans are spread but reproducible and
testable. The effective due time is therefore
`last_scan + effective_interval + offset`, where `effective_interval` is the
per-source `scan_interval_seconds` when set and `POLL_INTERVAL_SECONDS`
otherwise. The background scheduler wakes at the smallest effective due time
across enabled, non-tripped sources, so a jittered source is not rescanned until
its offset has elapsed and the loop does not busy-loop while it waits.

A value that is negative, non-integer, boolean or non-numeric is rejected with a
`400` naming `scan_jitter_seconds`. Jitter is only consulted by the background
scheduler: `POST /api/check` and `scan_all()` ignore both the intervals and the
jitter and scan every healthy enabled source immediately.

```sh
# Spread 50 shared-upstream sources over a 5-minute window instead of
# firing them all on the same tick, while keeping a 15-minute cadence.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Payments API","url":"https://status.payments.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Payments","tracked_versions":["v1"],
       "scan_interval_seconds":900,"scan_jitter_seconds":300}'
```

## Circuit breaker

Every source records its consecutive failed scans in `failure_count` and, once
it has failed too many times in a row, a `breaker_until` timestamp. The
semantics are:

- A scan that raises a `FetchError` (network error, non-`2xx` response, timeout,
  DNS failure) or that yields a malformed OpenAPI document increments
  `failure_count`. A malformed document is a parse failure, not a silent no-op.
- When `failure_count` reaches `BREAKER_THRESHOLD` (default `5`, minimum `1`)
  the source is *tripped*: `breaker_until` is set to
  `now + BREAKER_COOLDOWN_SECONDS` (default `1800`, minimum `60`) and the scan
  result records the trip once.
- While `breaker_until` is in the future the source is quarantined. The
  background scheduler excludes it from the scan set and does not let its
  (potentially long) remaining cooldown delay the wake-up for healthy sources;
  `Engine.scan_all()` — used by `POST /api/check` — also skips it and reports it
  under `sources_skipped` instead of `sources_failed`. A manual check therefore
  covers every *healthy* enabled source without hammering a quarantined one.
- A single successful fetch and parse resets `failure_count` to `0` and clears
  `breaker_until`, so the source is scanned again on the next tick. An operator
  can force the same reset at any time with the token-protected
  `POST /api/sources/:id/reset` endpoint, which returns the updated source and
  `404` for an unknown id.

The breaker state is part of the source JSON: `failure_count` (integer) and
`breaker_until` (ISO-8601 UTC or `null`). `GET /api/stats` adds
`sources_tripped`, the number of sources whose `breaker_until` is still in the
future. The dashboard renders a `Health` column (`ok`, `N failures` or
`tripped until <time>`) and offers a `Reset` action for tripped sources without
ever exposing header values or credentials.

## Fetch size limits

A multi-megabyte JSON changelog, a statuspage bundle or a runaway OpenAPI
document must not be able to exhaust memory or bloat the SQLite file. Every
fetch therefore reads at most a configurable number of bytes:

- `FETCH_MAX_BYTES` is the deployment-wide cap (positive integer, default
  `2000000`, i.e. 2 MB, floored at `1024`). A non-numeric value falls back to
  the default with a warning instead of crashing at startup.
- The optional per-source `max_bytes` overrides the global cap for one
  dependency. It is a positive integer or `null`/omitted; `0`, negatives,
  booleans, floats and strings are rejected with a `400` naming `max_bytes`.

The fetcher reads at most the effective cap and returns whether the body was
truncated. When the response advertises a `Content-Length` larger than the cap
the fetch fails with a `FetchError` unless the caller explicitly opts in, so a
hostile feed is refused rather than silently clipped. The engine always opts in:
when a body is clipped it still parses the partial body so a large feed degrades
gracefully, logs a warning and records the truncation on the `ScanResult`
(`sources_truncated` and `truncated_sources`) *without* incrementing
`failure_count` or tripping the circuit breaker. The last scan's truncation is
persisted per source and never stores or renders the raw body. It is surfaced
as `sources_truncated` in `GET /api/stats` and as a compact `truncated` badge in
the dashboard's `Health` column; the per-source `truncated` boolean and the
effective `max_bytes` are also part of the source JSON.

```sh
# Cap one noisy feed at 512 KB even though the deployment default is larger.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Huge changelog","url":"https://api.acme.com/changelog.json",
       "kind":"json","api_name":"Acme","max_bytes":524288}'
```

## Routing

The optional `notify_channels` field on a source selects which notifier channels
receive its alerts. It is a list whose entries are `console`, `slack`, `email`,
`webhook` or the literal `all`:

- omitted or `all` (the default) → every configured notifier, the original
  fan-out behaviour;
- `["slack"]` → only the Slack notifier;
- `["email"]` → only the e-mail notifier;
- `["webhook"]` → only the generic webhook notifier;
- `["console", "slack"]` → the console and Slack notifiers;
- `[]` (empty list) → treated as `all`.

Unknown entries are rejected with a `400` that names the allowed values (the
message lists `webhook`). The deployment-wide `NOTIFY_CHANNELS` environment
variable supplies the default for sources that omit the field, so an operator can
change the default without editing every source. The engine honours the field for
both immediate dispatch and digest delivery: a notifier is only invoked when at
least one alert is routed to it, and an alert is only marked notified once a
routed notifier has accepted it, so routing never silently drops an alert.

The optional `notify_webhook_url` field on a source overrides the deployment-wide
`WEBHOOK_URL` for that source. It must be an `http(s)` URL when present and is
rejected with a `400` naming the field otherwise. The engine resolves the target
URL per alert: sources with an override are delivered to their own endpoint,
while sources without one fall back to `WEBHOOK_URL`. The same grouping is
applied to digests, so a digest built for a source that sets an override is
POSTed to that URL rather than the global one.

The `webhook` notifier POSTs a small JSON body and treats anything other than a
`2xx` response — or a network error — as a `NotificationError`. That error is
logged and leaves the alert pending (`notified = 0`) so the next scan or digest
retries it without aborting the run. The payload shape is:

```json
{
  "alerts": [ { "id": 1, "source_id": 1, "severity": "critical", "reason": "v1 is deprecated", "...": "..." } ],
  "source_names": { "1": "Stripe" }
}
```

and, for digests, `{"digest": { "alert_count": 3, "severity": "warning", "...": "..." }}`.

The optional `headers` field on a source is a JSON object mapping header names to
string values. The fetcher merges it over the default `User-Agent`/`Accept`
headers, so a token-gated feed can be registered with e.g.
`{"X-Api-Key": "secret"}`. Header names are normalized case-insensitively to a
canonical `Title-Case` form (`user-agent` and `User-Agent` collapse to the same
key, last write wins). A header name that is empty or contains control
characters/newlines, or a non-string or newline-containing value, is rejected
with a `400` naming `headers`.

The optional `auth` field is a convenience for HTTP Basic auth. It must be a
`"user:password"` string with exactly one `:` and non-empty parts; the fetcher
adds `Authorization: Basic <base64(user:password)>` when it is set. An invalid
value (no colon, or an empty user/password) is rejected with a `400` naming
`auth`. Credentials are stored in SQLite in plain text and returned by the API,
so they must be protected like any other secret (restrict the data volume,
`API_TOKEN` and network access). Neither field is logged.

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

# Critical dependency: escalate to a dedicated incident endpoint via webhook.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Payments API","url":"https://status.payments.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Payments","tracked_versions":["v1"],
       "min_severity":"warning","notify_channels":["webhook"],
       "notify_webhook_url":"https://incidents.example/hooks/watchdog-critical"}'

# Token-gated feed: send a custom header on every fetch of this source.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Private status","url":"https://status.corp.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Internal",
       "headers":{"X-Api-Key":"secret-token","Accept":"application/json"}}'

# HTTP Basic auth: the fetcher sends Authorization: Basic <base64(user:pass)>.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Self-managed GitLab","url":"https://gitlab.corp.example/group/project",
       "kind":"gitlab","api_name":"Widget","auth":"bot:glpat-secret"}'
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

*Shipped:* **per-source scan jitter** (see *Current capabilities* and
*Scan cadence*) — an optional `scan_jitter_seconds` that adds a deterministic,
id-derived offset to a source's due time, so a large registry spreads its polls
across a window instead of firing every source on the same tick. This reduces
thundering-herd pressure on shared upstreams, composes with both
`scan_interval_seconds` and `POLL_INTERVAL_SECONDS`, leaves the default
behaviour byte-for-byte unchanged, and is ignored by the manual
`POST /api/check` path.

*Shipped:* **generic webhook adapter** (see *Current capabilities*) — a
`webhook` notifier channel that POSTs a dependency-free JSON payload to
`WEBHOOK_URL`, plus a per-source `notify_webhook_url` override so a critical
dependency can escalate to a dedicated incident endpoint. This advances the
*Integrations* item below: alert routing no longer requires a hard-coded
integration for each tracker or paging system.

*Shipped:* **per-source request headers and basic auth** (see *Current
capabilities*) — an optional `headers` map and an `auth` (`"user:password"`)
field on each source, merged over the default request headers with an
`Authorization: Basic` header added when `auth` is set, so authenticated and
otherwise gated feeds (private GitHub Enterprise/GitLab, token-gated
statuspages, internal changelogs) can be monitored without forking the service.

*Shipped:* **per-source failure circuit breaker** (see *Current capabilities*) —
`failure_count` and `breaker_until` on each source, a `BREAKER_THRESHOLD` /
`BREAKER_COOLDOWN_SECONDS` policy, skip-rather-than-retry semantics in the
scheduler and `Engine.scan_all()`, a token-protected
`POST /api/sources/:id/reset`, a `sources_tripped` stat and a dashboard
  `Health` column, so one flaky or hostile dependency can no longer starve the
  shared poll loop.

*Shipped:* **JSON data export** (see *Current capabilities* and *Data export*) —
a token-free, read-only `GET /api/export` that returns one JSON snapshot of
`stats`, `sources`, `entries`, `alerts` and `digests` with an independent
`?limit=` (default `1000`, max `10000`), plus a `Download JSON` dashboard link.
This gives teams an offline-analysis and incident-report path without a custom
client and is a first step toward the *Metrics export* item below.

*Shipped:* **per-source keyword overrides** (see *Current capabilities* and
*Keyword overrides*) — optional `extra_keywords` and `suppress_keywords` lists
on each source that extend or mute the relevance vocabulary for that dependency,
so a vendor that uses `"EOL"`, `"retired"` or an internal phrase is detected
without forking the service and a noisy term can be silenced without losing the
rest of the vocabulary. Both lists default to `[]`, preserving the legacy
behaviour exactly. This is the next increment on top of the relevance engine and
advances the *Smarter relevance* item below.

*Shipped:* **per-source fetch size limits** (see *Current capabilities* and
*Fetch size limits*) — a deployment-wide `FETCH_MAX_BYTES` and an optional
per-source `max_bytes` cap how much of each response body is read and stored,
the engine parses a clipped body gracefully instead of dropping it or tripping
the breaker, and the `truncated` signal reaches the API, stats and dashboard.

1. **More source adapters:** additional structured feeds beyond the shipped
   GitHub and GitLab releases and OpenAPI adapters. *Partially shipped:*
   the `gitlab` adapter now covers GitLab-hosted releases; further forges and
   trackers remain.
2. **Smarter relevance:** semantic matching of version ranges (semver / date
   versions), per-team severity rules and richer keyword tuning. *Partially
   shipped:* per-source `min_severity` and `notify_channels` already let a team
   tune the volume and destination of alerts per dependency, and the shipped
   per-source `extra_keywords` / `suppress_keywords` overrides let a team extend
   or mute the detection vocabulary without forking the service.
3. **Integrations:** GitHub/GitLab issues and Jira as delivery targets.
   *Partially shipped:* per-source `notify_channels` routes alerts to the
   existing Slack, e-mail, console and generic `webhook` channels, and the
   webhook adapter lets any internal system be wired up without a hard-coded
   integration; first-class tracker integrations remain to be built.
4. **Multi-tenant onboarding:** organizations, API keys and shared source
   templates for the manual concierge phase.
5. **Metrics export:** Prometheus endpoint for adoption and reliability
   monitoring. *Partially shipped:* the JSON `GET /api/export` endpoint already
   exposes `stats` and every stored collection for offline tooling; a
   Prometheus-format scrape endpoint remains to be built.

## Architecture

```
sources ──▶ fetcher ──▶ parsers ──▶ relevance ──▶ store (SQLite)
                          │                     └─▶ notifiers (Slack / e-mail / webhook / console)
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
or `POLL_INTERVAL_SECONDS`) plus the deterministic `scan_jitter_seconds` offset
and calls `engine.scan_sources` only for the sources that are due. It wakes at
the smallest effective due time among the enabled, non-tripped sources, so the
jitter spreads load without delaying a due source indefinitely or busy-looping.
`engine.scan_all` remains the "scan everything now" primitive used by the manual
check endpoint, so the two entry points share one scanning implementation while
keeping different cadence semantics; `scan_all` consults neither intervals nor
jitter. Both entry points skip sources whose circuit breaker is still tripped.

## Constraints and risks

- Depends on the structure and availability of external changelog sources;
  parsing is heuristic and can degrade when a source changes layout.
- Source formats vary widely, which is why per-source kinds and selectors
  exist.
- Teams may ignore deprecation warnings; prioritization and low noise are the
  mitigation.
- Integrating with private repositories or trackers raises access/privacy
  questions. Per-source `headers`/`auth` credentials are stored in SQLite in
  plain text, so the database volume, `API_TOKEN` and network access must be
  protected like any other secret.
