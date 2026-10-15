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

- **Source registry** for RSS/Atom, JSON, HTML, statuspage.io, GitHub and
  GitLab releases and OpenAPI/Swagger specs (`rss`, `json`, `html`,
  `statuspage`, `github`, `gitlab`, `openapi`).
- **Parsers** for all seven formats, with no third-party dependencies. The
  `github` kind accepts either `https://api.github.com/repos/<owner>/<repo>/releases`
  or the repository HTML URL `https://github.com/<owner>/<repo>`, which is
  normalized to the API URL on registration. The `gitlab` kind mirrors this:
  it accepts the releases API URL
  `https://gitlab.com/api/v4/projects/<id-or-path>/releases` or the project
  HTML URL `https://gitlab.com/<group>/<project>`, normalized to the API URL.
- **OpenAPI diff detection**: the `openapi` kind diffs two spec versions (from
  a selected list) and emits one entry per structural change — removed paths or
  operations, narrowed enums, added required parameters — with a severity
  derived from the kind of change rather than from changelog prose.
- **Per-source selectors** for precise extraction: a CSS-like selector for
  `html` (`h2.deprecation`, `#changelog`) and a dotted path for `json`,
  `github`, `gitlab` and `openapi` (`data.releases`, `versions`).
- **Relevance engine** that detects deprecation / breaking-change language and
  boosts entries referencing tracked versions; classifies hits as `critical`,
  `warning` or `info` (the severity ladder, highest to lowest).
- **Per-source severity floor**: each source has a `min_severity` (default
  `info`) that suppresses alerts below the chosen level while still recording
  the entries.
- **Per-source keyword overrides**: each source can carry `extra_keywords` and
  `suppress_keywords` lists that extend or mute the relevance vocabulary for
  that dependency alone — for example `["retired", "eol"]` or
  `["end of support"]` for a vendor that never says "end of life", and a term
  such as `"notice"` to silence it. Extra terms are matched case-insensitively
  and raise an entry to `critical`; suppressed terms are removed before
  classification. Both default to `[]`, which preserves the built-in
  vocabulary exactly.
- **Per-source scan intervals**: each source has an optional
  `scan_interval_seconds` that overrides the global `POLL_INTERVAL_SECONDS`
  cadence, so a volatile dependency can be polled every 15 minutes while a
  stable SDK is checked once a day. Omitting it keeps the global cadence; a
  manual `POST /api/check` always scans every enabled source.
- **Fetch size limits & truncation reporting**: every fetch reads at most
  `FETCH_MAX_BYTES` (default `2000000`, i.e. 2 MB, floored at `1024`) bytes, and
  each source can override that with a positive `max_bytes`. A clipped body is
  still parsed so a large feed degrades gracefully instead of being dropped or
  tripping the circuit breaker, the truncation is logged and reported as
  `sources_truncated` in `GET /api/stats`, and the dashboard shows a compact
  `truncated` badge in the `Health` column. The raw body is never stored beyond
  the normal 20 KB entry cap or rendered on the dashboard.
- **Per-source request headers & basic auth**: each source can send custom HTTP
  `headers` (for example `{"X-Api-Key": "secret"}`) and an `auth`
  (`"user:password"`) convenience field that adds
  `Authorization: Basic <base64>`. This unlocks private GitHub Enterprise or
  GitLab instances, token-gated statuspages and internal changelogs. Header
  names are normalized case-insensitively (`user-agent` == `User-Agent`); the
  dashboard shows only a lock/header-count indicator, never the secret values.
- **Per-source failure circuit breaker**: each source tracks `failure_count`
  (consecutive failed scans) and `breaker_until`. After `BREAKER_THRESHOLD`
  failures a source is quarantined for `BREAKER_COOLDOWN_SECONDS`, so a flaky or
  hostile feed is skipped (reported as `sources_skipped`) instead of retried on
  every tick. A successful scan — or a call to `POST /api/sources/:id/reset` —
  clears the counter and breaker.
- **SQLite history** with content-hash de-duplication (no repeated alerts).
- **Notifications** via Slack webhook, SMTP e-mail, a generic JSON webhook and
  console.
- **Per-source notification routing** via a `notify_channels` list, so a chatty
  `info`-only source can go to Slack only while a critical dependency escalates
  to e-mail or a webhook. `all` (the default) keeps the fan-out to every
  notifier.
- **Generic webhook delivery** that POSTs a dependency-free JSON payload to
  `WEBHOOK_URL`, so any internal system can consume alerts. A per-source
  `notify_webhook_url` overrides the global URL for one dependency's alerts and
  digests.
- **Digest notifications** that batch alerts into a single grouped, severity-
  ordered summary on a configurable cadence (`off`, `digest` or `both`).
- **Web dashboard** with an `Interval` column showing each source's cadence (or
  the global default), an `Auth` column showing a lock/header-count indicator
  (never the secret values), a `Keywords` column showing a compact
  `+extra / -suppressed` count (`–` when both lists are empty), a `Health`
  column (`ok`, `N failures`, `truncated` for a clipped last scan, or
  `tripped until <time>` with a `Reset` action), a
  JSON API and a `Download JSON` link that snapshots the full state through
  `/api/export`.
- **JSON data export**: a read-only `GET /api/export` returns one document with
  `generated_at`, `stats`, `sources`, `entries`, `alerts` and `digests` for
  offline analysis, incident reports or feeding other tools. An optional
  `?limit=` (default `1000`, maximum `10000`) caps entries, alerts and digests
  independently; sources are always complete.
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
| `POLL_INTERVAL_SECONDS`  | `3600`  | Global background scan interval; per-source `scan_interval_seconds` overrides it |
| `FETCH_TIMEOUT_SECONDS`  | `20`    | Per-source HTTP timeout                             |
| `FETCH_MAX_BYTES`        | `2000000` | Maximum response body bytes read and stored per fetch (floored at `1024`); per-source `max_bytes` overrides it |
| `BREAKER_THRESHOLD`      | `5`     | Consecutive scan failures before a source is tripped (minimum `1`) |
| `BREAKER_COOLDOWN_SECONDS` | `1800` | Quarantine window after tripping a source (minimum `60`) |
| `DISABLE_SCHEDULER`      | `false` | Disable background polling                          |
| `SEED_SOURCES`           | `true`  | Seed default sources on first start                 |
| `NOTIFY_CHANNELS`        | `all`   | Default routing for sources that omit `notify_channels`; comma-separated `console`, `slack`, `email` or `all` |
| `API_TOKEN`              | –       | Bearer token required for mutating endpoints        |
| `SLACK_WEBHOOK_URL`      | –       | Slack incoming webhook for alert delivery           |
| `WEBHOOK_URL`            | –       | Generic JSON webhook for the `webhook` channel; per-source `notify_webhook_url` overrides it |
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
| GET    | `/api/sources`     | List monitored sources (incl. `min_severity`, `extra_keywords`, `suppress_keywords`, `max_bytes`, `truncated`, `failure_count`, `breaker_until`) |
| POST   | `/api/sources`     | Register a source (token protected)       |
| GET    | `/api/sources/:id` | Fetch one source (incl. `extra_keywords`, `suppress_keywords`, `max_bytes`, `truncated`, `failure_count`, `breaker_until`) |
| GET    | `/api/sources/:id/entries` | List one source's entries, newest first (`?limit=`) |
| POST   | `/api/sources/:id/reset` | Clear one source's breaker state (token protected) |
| DELETE | `/api/sources/:id` | Remove a source (token protected)         |
| GET    | `/api/alerts`      | List alerts (`?limit=`, `?severity=`)     |
| GET    | `/api/entries`     | List parsed entries (`?source_id=`)       |
| GET    | `/api/digests`     | List digests, newest first (`?limit=`)    |
| GET    | `/api/digests/:id` | Fetch one digest (`404` when unknown)     |
| GET    | `/api/stats`       | Aggregate counters (incl. `sources_tripped`) |
| GET    | `/api/export`      | Full JSON snapshot of sources, entries, alerts and digests (`?limit=`) |
| POST   | `/api/check`       | Run a scan now (token protected)          |
| POST   | `/api/digest/flush`| Build and deliver a digest (token protected) |

Register a source. The `POST /api/sources` payload accepts:

| Field              | Required | Purpose                                                       |
| ------------------ | -------- | ------------------------------------------------------------- |
| `name`             | yes      | Display name                                                  |
| `url`              | yes      | `http(s)` URL to fetch                                         |
| `kind`             | yes      | `rss`, `json`, `html`, `statuspage`, `github`, `gitlab` or `openapi` |
| `api_name`         | no       | API/SDK the source documents                                  |
| `tracked_versions` | no       | Versions to boost during relevance scoring                    |
| `selector`         | no       | Extraction hint; CSS-like for `html`, dotted path for `json`/`github`/`gitlab`/`openapi` |
| `enabled`          | no       | Whether the scheduler polls it (default `true`)               |
| `min_severity`     | no       | Lowest severity that creates an alert: `critical`, `warning` or `info` (default `info`) |
| `notify_channels`  | no       | Channels that receive this source's alerts: a list of `console`, `slack`, `email`, `webhook` or `all` (default `all`) |
| `notify_webhook_url` | no     | `http(s)` URL that overrides `WEBHOOK_URL` for this source's alerts and digests |
| `scan_interval_seconds` | no  | Positive integer override for this source's polling cadence; omitted keeps the global `POLL_INTERVAL_SECONDS` |
| `max_bytes`        | no       | Positive integer cap on the response body bytes read and stored for this source; omitted or `null` uses the global `FETCH_MAX_BYTES` |
| `headers`          | no       | JSON object of header name to string value sent on every fetch (merged over the defaults; names normalized case-insensitively) |
| `auth`             | no       | `"user:password"` HTTP Basic credentials; adds `Authorization: Basic <base64>` |
| `extra_keywords`   | no       | List of terms that extend the relevance vocabulary for this source and raise a matching entry to `critical` (trimmed, lower-cased, default `[]`) |
| `suppress_keywords`| no       | List of terms removed from the matched set before classification, so a noisy phrase can be muted per source (trimmed, lower-cased, default `[]`) |

For `html`, `selector` is a CSS-like simple selector (optional tag, `#id`,
`.class`, or a combination). It matches a block when the block or an ancestor
satisfies it, so `#changelog` scopes extraction to that container. For `json`,
`github`, `gitlab` and `openapi`, `selector` is a dotted path such as
`data.releases` or `versions`.

For `github` sources, `url` may be either the releases REST API URL
(`https://api.github.com/repos/<owner>/<repo>/releases`) or the repository HTML
URL (`https://github.com/<owner>/<repo>`, with or without a trailing `.git`).
The HTML form is normalized to the API URL when the source is stored. Each
release becomes an entry whose title is the release `name` or `tag_name`, body
is the release `body`, link is `html_url`, date is `published_at`, and whose
categories include the `tag_name` and, when set, `prerelease`; draft releases
are skipped.

For `gitlab` sources, `url` may be either the releases REST API URL
(`https://gitlab.com/api/v4/projects/<id-or-path>/releases`) or the project
HTML URL (`https://gitlab.com/<group>/<project>`, with or without a trailing
`.git` or `/`; nested subgroups are supported). The HTML form is normalized to
`https://gitlab.com/api/v4/projects/<group>%2F<project>/releases` when the
source is stored, while an existing API URL is left unchanged. Each release
becomes an entry whose title is the release `name` or `tag_name`, body is the
release `description`, link is `_links.self`, date is `released_at`, and whose
categories include the `tag_name` and, when set, `upcoming_release`. GitLab has
no draft concept, so no release is skipped.

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

`notify_channels` controls where a source's alerts are delivered. It is a list
of `console`, `slack`, `email`, `webhook` or the literal `all`; omitting the
field defaults to `all`, which fans every alert out to every configured notifier
(the legacy behaviour). An empty list is treated as `all`. The same routing
applies to digest delivery: each notifier receives a digest containing only the
alerts routed to it. Set `NOTIFY_CHANNELS` to change the default for sources that
omit the field. Unknown channel names are rejected with a `400` that lists the
allowed channels, including `webhook`.

The `webhook` channel POSTs JSON to `WEBHOOK_URL` (configure it to enable the
channel). Alerts are delivered as:

```json
{
  "alerts": [
    {
      "id": 1,
      "source_id": 1,
      "entry_id": 1,
      "severity": "critical",
      "reason": "v1 is deprecated and will be removed",
      "matched_terms": ["deprecated"],
      "version_hits": ["v1"],
      "title": "v1 deprecated",
      "link": "https://example.com/changelog#v1",
      "notified": false,
      "digest_id": null,
      "created_at": "2024-05-01T00:00:00+00:00"
    }
  ],
  "source_names": { "1": "Stripe" }
}
```

Digests are delivered as `{"digest": { ... }}`, where the object is the digest's
JSON form (`alert_count`, `severity`, `subject`, `body`, `omitted`, `delivered`,
`created_at`). A non-`2xx` response or a network error raises
`NotificationError`; the engine logs it and leaves the alert pending
(`notified = 0`) so the next scan or digest retries it.

`notify_webhook_url` overrides `WEBHOOK_URL` for one source. It must be an
`http(s)` URL or omitted; an invalid value is rejected with a `400` naming the
field. Sources without an override keep the global `WEBHOOK_URL`, so a critical
dependency can post to a dedicated incident endpoint while a chatty source uses
the shared one.

`scan_interval_seconds` overrides the global `POLL_INTERVAL_SECONDS` cadence for
one source. It is a positive integer number of seconds; omitting it (or sending
`null`) leaves the source on the global interval, so existing registrations and
databases keep their behaviour. A zero, negative, non-integer or non-numeric
value is rejected with a `400` naming the field. The background scheduler tracks
the last scan time per source and only scans a source once its effective
interval has elapsed, waking at the smallest effective interval among the
enabled sources so a 15-minute source is not throttled by an hourly global
timer. Both the per-source value and the global default are floored at 30
seconds. A manual `POST /api/check` ignores the intervals and scans every
enabled source in one run.

`max_bytes` caps how much of a response body is read and stored for one source.
It is a positive integer number of bytes; omitting it (or sending `null`) uses
the deployment-wide `FETCH_MAX_BYTES` (default `2000000`, i.e. 2 MB, floored at
`1024`). A zero, negative, non-integer, boolean or string value is rejected with
a `400` naming the field. When a response advertises a `Content-Length` larger
than the effective cap the fetch fails with a `FetchError` unless the caller
opts in; the engine always opts in, so a clipped body is still parsed and the
source is not treated as failed. A truncated scan logs a warning, reports
`sources_truncated` (and `truncated_sources`) on the `POST /api/check` result and
in `GET /api/stats`, sets the source's `truncated` flag, and shows a
compact `truncated` badge in the dashboard's `Health` column without ever
exposing the body.

```sh
# Cap one outsized feed at 512 KB while the deployment default stays at 2 MB.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Huge changelog","url":"https://api.acme.com/changelog.json",
       "kind":"json","api_name":"Acme","max_bytes":524288}'
```

`headers` sends extra HTTP headers on every fetch of that source. It is a JSON
object of string-to-string entries, merged over the default `User-Agent` and
`Accept` headers, so a token-gated or custom-`Accept` feed can be monitored
without forking. Header names are normalized case-insensitively to a canonical
`Title-Case` form, so `user-agent` and `User-Agent` collapse to the same key and
the last write wins. A non-object `headers`, a header name that is empty or
contains control characters/newlines, or a non-string/newline-containing value
is rejected with a `400` naming `headers`.

`auth` is a convenience for HTTP Basic auth. It must be a `"user:password"`
string with exactly one `:` and non-empty parts; the fetcher then sends
`Authorization: Basic <base64(user:password)>` on every fetch. A value without a
colon, or with an empty user or password, is rejected with a `400` naming
`auth`. Neither the header values nor the credentials are ever logged or
rendered in the dashboard (which shows only a lock/header-count indicator).

> **Warning:** `headers` and `auth` are stored in the SQLite database in plain
> text and are returned by the API. Protect the data volume, set `API_TOKEN`
> and restrict network access to the service just as you would for any other
> secret.

`extra_keywords` and `suppress_keywords` tune the relevance vocabulary for one
source. Each is an optional list of non-empty strings (default `[]`, which keeps
the built-in vocabulary unchanged). Terms are trimmed and lower-cased when the
source is registered, and a non-list value or a non-string/blank entry is
rejected with `400 {"error": "'extra_keywords' must be a list of strings"}` (the
message names the offending field). `extra_keywords` extends the built-in
critical terms: an entry that would otherwise be low-signal is raised to
`critical` when it contains one of the terms (matched case-insensitively on word
boundaries). `suppress_keywords` removes the matching built-in terms *before*
classification, so an entry whose only match was suppressed is no longer
relevant and creates no alert. Both fields are persisted in the `sources` table
and returned by `GET /api/sources` and `GET /api/sources/:id`; databases created
before this feature are migrated automatically and read back as `[]`.

```sh
# A vendor that says "end of support" and "EOL" rather than "end of life":
# promote those terms and mute the recurring "notice" noise.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Vendor SDK","url":"https://vendor.example/changelog.json",
       "kind":"json","api_name":"Vendor","tracked_versions":["v2"],
       "extra_keywords":["retired","eol"],"suppress_keywords":["notice"]}'
```

`GET /api/sources/:id/entries` returns the entries recorded for one source,
newest first (ordered by `created_at` then `id`, both descending) and capped by
the optional `?limit=` query parameter (default `100`). An unknown `:id` returns
`404 {"error": "source not found"}`, while a non-integer `:id` or `?limit=`
returns `400 {"error": "invalid id"}` or `{"error": "invalid limit"}`
respectively. The route matches the `/entries` suffix, so `GET` and
`DELETE /api/sources/:id` are unchanged.

```sh
# The stored history for source 1, newest first, at most 20 entries.
curl "http://localhost:8080/api/sources/1/entries?limit=20"
```

### Export

`GET /api/export` returns a single read-only JSON document containing the whole
watchdog state, so you can snapshot it for offline analysis, incident reports or
feed it into other tools without writing a custom client. It requires no token
(like the other read endpoints) and never mutates the store.

```sh
# Download the full state (up to 1000 entries, alerts and digests).
curl http://localhost:8080/api/export > watchdog-export.json

# Cap each collection independently while still returning every source.
curl "http://localhost:8080/api/export?limit=5"
```

The payload keys are:

| Key            | Contents                                                       |
| -------------- | ------------------------------------------------------------- |
| `generated_at` | Snapshot time as an ISO-8601 UTC timestamp                     |
| `stats`        | The same aggregate counters as `GET /api/stats`                |
| `sources`      | Every source, in full, as its `to_dict()` form                 |
| `entries`      | Parsed entries, newest first, capped by `?limit=`              |
| `alerts`       | Alerts, newest first, capped by `?limit=`                      |
| `digests`      | Digests, newest first, capped by `?limit=`                     |

`?limit=` defaults to `1000`, must be an integer between `1` and `10000`, and is
applied to `entries`, `alerts` and `digests` independently; `sources` are always
returned in full. A non-integer or out-of-range value returns
`400 {"error": "invalid limit"}`. The dashboard header also offers a
`Download JSON` link to the same endpoint.

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

# GitLab releases: paste the project URL, it is normalized to the releases API.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Widget SDK (GitLab)","url":"https://gitlab.com/acme/widget",
       "kind":"gitlab","api_name":"Widget","tracked_versions":["v2"],
       "min_severity":"warning"}'

# OpenAPI spec: diff the versions array and alert on warning+ structural changes.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme API spec","url":"https://api.acme.com/openapi.json",
       "kind":"openapi","api_name":"Acme","tracked_versions":["v2"],
       "selector":"versions","min_severity":"warning"}'

# Chatty info-only source: route to Slack only, keep it out of e-mail.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Release Notes","url":"https://api.acme.com/changelog.json",
       "kind":"json","api_name":"Acme","selector":"data.releases",
       "min_severity":"info","notify_channels":["slack"]}'

# Critical dependency: escalate to e-mail only, no Slack noise.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Payments API","url":"https://status.payments.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Payments","tracked_versions":["v1"],
       "min_severity":"warning","notify_channels":["email"]}'

# Volatile dependency: poll every 15 minutes instead of the global hour.
# The scheduler scans it four times as often as a source with no interval.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Payments API","url":"https://status.payments.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Payments","tracked_versions":["v1"],
       "min_severity":"warning","scan_interval_seconds":900}'

# Critical dependency: POST its alerts to a dedicated incident endpoint.
# Sources without the override keep using the global WEBHOOK_URL.
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

# HTTP Basic auth: fetches send Authorization: Basic <base64(user:pass)>.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Self-managed GitLab","url":"https://gitlab.corp.example/group/project",
       "kind":"gitlab","api_name":"Widget","auth":"bot:glpat-secret"}'
```

## Failure circuit breaker

A single slow or permanently broken source must not starve the shared poll loop.
Every source therefore records two extra fields, returned by `GET /api/sources`
and `GET /api/sources/:id`:

| Field            | Meaning                                                       |
| ---------------- | ------------------------------------------------------------- |
| `failure_count`  | Number of consecutive failed scans (integer, default `0`)     |
| `breaker_until`  | ISO-8601 UTC timestamp until which the source is quarantined, or `null` |

A scan is a *failure* when the fetch raises a `FetchError` (network error,
non-`2xx` response, timeout) or when an `openapi` document cannot be parsed. Each
failure increments `failure_count`; once it reaches `BREAKER_THRESHOLD` (default
`5`, minimum `1`) the source is *tripped* and `breaker_until` is set to
`now + BREAKER_COOLDOWN_SECONDS` (default `1800`, minimum `60`). While tripped:

- the background scheduler excludes the source and does not let its remaining
  cooldown delay the wake-up for healthy sources;
- `POST /api/check` (`Engine.scan_all()`) skips it and reports it under
  `sources_skipped` rather than `sources_failed`, so a manual check still covers
  every healthy enabled source;
- `GET /api/stats` counts it in `sources_tripped`.

A single successful fetch and parse resets `failure_count` to `0` and clears
`breaker_until`, so the source is scanned again on the next tick. Operators can
force the same reset immediately:

```sh
# Clear a source's failure count and breaker (token protected when API_TOKEN is set).
curl -X POST http://localhost:8080/api/sources/1/reset \
  -H "Authorization: Bearer $API_TOKEN"
# -> the updated source, or 404 {"error": "source not found"} for an unknown id
```

The dashboard shows a `Health` column — `ok`, `N failures` or
`tripped until <time>` — with a `Reset` button for tripped sources. It never
renders header values or credentials.

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
`config.digestIntervalSeconds` and `config.digestMaxAlerts` in `values.yaml`;
the circuit breaker through `config.breakerThreshold` and
`config.breakerCooldownSeconds`; and the fetch size cap through
`config.fetchMaxBytes` (`FETCH_MAX_BYTES`).

```sh
helm install watchdog deploy/helm/api-deprecation-watchdog \
  --set image.repository=ghcr.io/your-org/api-deprecation-watchdog \
  --set image.digest=sha256:... \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=watchdog.example.com \
  --set existingSecret=watchdog-secrets
```

`existingSecret` should provide `API_TOKEN`, `SLACK_WEBHOOK_URL`, `WEBHOOK_URL`
and `SMTP_*` keys. `.github/workflows/factory-release.yml` builds and publishes the multi-arch
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
