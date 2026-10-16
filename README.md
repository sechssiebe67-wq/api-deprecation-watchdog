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
- **Per-source OpenAPI baseline & ignore paths**: an `openapi` source can pin an
  `openapi_baseline` (a JSON spec object, default `null`) so the live document
  is diffed against that spec instead of the empty one — the first scan then
  reports only the changes since the pinned spec rather than the whole surface.
  It can also list `openapi_ignore_paths` (dotted path or path-prefix strings,
  default `[]`, for example `paths./internal/health` or `paths./v2/legacy`) to
  mute known-irrelevant structural changes: matching is by exact path or prefix
  on a `/` boundary, so `paths./v2/legacy` also covers `paths./v2/legacy/sub`.
  Both default to off, so a source with neither field set behaves exactly as
  before; the dashboard shows a compact `baseline` / `ignore N` indicator and
  never renders the pinned payload.
- **Per-source status-page component filter**: a `statuspage` source can carry a
  `statuspage_components` list (component names or ids, default `[]`) so only
  incidents and scheduled maintenances affecting the components a team actually
  depends on are ingested. A `components[].name` is matched case-insensitively
  (so `api`, `Api` and `API` all match) and a `components[].id` exactly (so
  `abc123` does not match `ABC123`); an item that names no component is kept
  only when the list is empty, and omitting the field keeps the previous
  behaviour byte-for-byte. This stops a page that covers unrelated components
  (marketing site, billing, and the API you depend on) from eroding trust in the
  alert stream; the dashboard shows a compact `components N` indicator.
- **Per-source selectors** for precise extraction: a CSS-like selector for
  `html` (`h2.deprecation`, `#changelog`) and a dotted path for `json`,
  `github`, `gitlab` and `openapi` (`data.releases`, `versions`). Dotted paths
  also accept array indices — `0`, `1`, … from the front and `-1` for the last
  element — so a list nested at a fixed position is reachable
  (`data.changelogs.0.entries`); invalid segments yield no entries instead of
  raising.
- **Relevance engine** that detects deprecation / breaking-change language and
  boosts entries referencing tracked versions; classifies hits as `critical`,
  `warning` or `info` (the severity ladder, highest to lowest).
- **Per-source severity floor**: each source has a `min_severity` (default
  `info`) that suppresses alerts below the chosen level while still recording
  the entries.
- **Per-source notification floor**: each source can set a `notify_min_severity`
  (default: inherit `min_severity`) as a second, notification-only floor.
  Alerts below it are still stored, appear in `/api/entries`, `/api/alerts` and
  `/api/export`, and keep `notified = 0`, but they are never fanned out by
  immediate dispatch and are never claimed by a digest flush. Set
  `min_severity: "info"` with `notify_min_severity: "critical"` to keep the full
  history while only notifying on critical changes; held-back alerts are counted
  as `alerts_suppressed`, and the dashboard shows a compact `notify: <severity>`
  indicator.
- **Per-source alert cooldown**: each source can set an
  `alert_cooldown_seconds` (non-negative integer, default none) so a noisy
  dependency whose changelog re-announces the same deprecation, or emits several
  near-identical entries in a short window, does not flood Slack, e-mail or the
  webhook channel. An alert is only dispatched when no alert from the same
  source was dispatched within the previous window; suppressed alerts are still
  stored with `notified = 0`, appear in `/api/alerts` and `/api/entries`, and
  are summarised by the next digest. The last dispatch time is persisted, so the
  window survives a restart, and the held-back count is reported as
  `alerts_suppressed` on the check result and in `GET /api/stats`.
- **Per-source alert-cooldown jitter**: each source can set an
  `alert_cooldown_jitter_seconds` (non-negative integer, default none) that
  spreads the effective `alert_cooldown_seconds` window by a deterministic,
  source-id-derived offset, so a large registry in which many sources share the
  same cooldown no longer releases their coalesced alerts on the same tick. The
  effective cooldown is
  `alert_cooldown_seconds + (source.id % (alert_cooldown_jitter_seconds + 1))`,
  mirroring `scan_jitter_seconds` (same derivation, no randomness, stable for a
  given `(id, jitter)` pair). It is notification-only — held-back alerts are
  stored with `notified = 0`, appear in `/api/alerts`, `/api/entries` and
  `/api/export`, stay eligible for a later digest and count as
  `alerts_suppressed` — and a source with no cooldown (unset or `0`) is
  unaffected. Omitting the field, sending `null` or `0` preserves the
  pre-feature behaviour byte-for-byte, and the dashboard appends a compact
  `+0-Ns` indicator when the jitter is set (for example `cooldown 300s +0-60s`).
- **Per-source title dedupe window**: each source can set a
  `dedupe_window_seconds` (non-negative integer, default none) so a vendor that
  re-posts the same deprecation announcement with a slightly changed body or a
  new `published` date does not produce a second notification. Unlike the
  cooldown, the window is keyed on the alert's normalized title (trimmed,
  whitespace-collapsed, case-folded), so a genuinely new title is still
  delivered inside another title's window. Held-back alerts are still stored
  with `notified = 0`, appear in `/api/alerts`, `/api/entries` and `/api/export`,
  stay eligible for a later digest, are counted as `alerts_suppressed` in
  `GET /api/stats`, and the per-title dispatch times are persisted so a restart
  does not reset the window. The dashboard shows a compact `dedupe Ns`
  indicator (or `–` when unset).
- **Per-source severity escalation window**: each source can set an
  `escalation_window_seconds` (non-negative integer, default none) so a source
  that re-announces the *same severity* of change every few days (with a
  drifting title and body, so the content hash and the title window both miss
  it) pages at most once per window. An alert at or above the source's effective
  notification floor is only dispatched when no alert of the same severity was
  dispatched from the same source within the window; a higher-severity alert
  always escalates through. Held-back alerts are still stored with
  `notified = 0`, appear in `/api/alerts`, `/api/entries` and `/api/export`,
  stay eligible for a later digest, and are counted as `alerts_suppressed` in
  `GET /api/stats`. It composes with `alert_cooldown_seconds`,
  `dedupe_window_seconds` and `notify_min_severity`, and the per-severity
  dispatch times are persisted so a restart does not reset the window. The
  dashboard shows a compact `escalate Ns` indicator (or `–` when unset).
- **Per-source per-scan alert fan-out cap**: each source can set a positive
  `max_alerts_per_scan` (default none) that bounds how many alerts are actually
  dispatched from one scan. A vendor that suddenly dumps dozens of genuinely
  distinct breaking changes, a flapping statuspage or an OpenAPI diff that
  surfaces many structural changes at once no longer floods Slack, e-mail and the
  webhook channel in a single tick: the alerts are prioritised by severity
  (`critical`, then `warning`, then `info`, stable on creation order) and only
  the top `N` fan out. The remainder are still stored with `notified = 0`, remain
  visible through `/api/entries`, `/api/alerts` and `/api/export` and stay
  eligible for a later digest flush. The number held back is counted as
  `alerts_suppressed` in `GET /api/stats`, and the dashboard shows a compact
  `fanout N` indicator (or `fanout –` when unset).
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
- **Per-source scan jitter**: each source has an optional
  `scan_jitter_seconds` (non-negative integer, default none). When set, the
  scheduler adds a deterministic offset in `[0, scan_jitter_seconds]`, derived
  from the source id, to that source's due time so a large registry spreads its
  polls instead of firing every source on the same tick. This reduces
  thundering-herd pressure on shared upstreams while staying reproducible (no
  randomness). The effective due time is
  `last_scan + effective_interval + offset`, composing with both
  `scan_interval_seconds` and `POLL_INTERVAL_SECONDS`; a manual
  `POST /api/check` ignores jitter and scans immediately.
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
- **Per-source entry-count cap**: each source can set a positive `max_entries`
  that bounds its stored history by count. After every successful scan the oldest
  entries beyond the cap are pruned (newest-first), so one chatty dependency
  cannot grow the SQLite file without bound even inside its retention window. It
  composes with `retention_days` — the tighter of the count and age bounds wins —
  and **never deletes alerts or digests**, so an alert whose entry was pruned
  still appears in `/api/alerts` with its stored title and link and
  `/api/digests` is unaffected. Omitted or `null` keeps the unbounded default,
  the dashboard shows a compact `cap N` indicator (or `cap –` when unset), and
  `GET /api/stats` reports the total removed as `entries_pruned`.
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
  the global default), its scan-jitter window when set (for example
  `900s +0-300s`), its `cooldown Ns` alert-cooldown indicator when set (with a
  `+0-Ns` alert-cooldown-jitter window appended when set, for example
  `cooldown 300s +0-60s`), its
  `dedupe Ns` title-dedupe indicator, its `escalate Ns` severity-escalation
  indicator and its `fanout N` per-scan alert fan-out cap indicator
  (each `–` when unset), an
  `Auth` column showing a lock/header-count indicator
  (never the secret values), a `Webhook` column showing `override` (never the
  URL, which may embed a token), a `Keywords` column showing a compact
  `+extra / -suppressed` count (`–` when both lists are empty), an `OpenAPI`
  extra column showing a compact `baseline` / `ignore N` indicator for an
  `openapi` source and a `components N` indicator for a filtered `statuspage`
  source (`–` when unset, and never the component list or the pinned payload), a
  `Health`
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
| `WEBHOOK_URL`            | –       | Generic JSON webhook for the `webhook` channel and its digests; per-source `notify_webhook_url` overrides it per source (omitted sources keep this global URL) |
| `SMTP_HOST` / `SMTP_PORT`| – / `587` | SMTP server for e-mail alerts                     |
| `SMTP_USER` / `SMTP_PASSWORD` | –  | SMTP credentials                                    |
| `SMTP_FROM` / `SMTP_TO`  | –       | Sender and comma-separated recipients               |
| `SMTP_STARTTLS`          | `true`  | Use STARTTLS                                        |
| `DIGEST_MODE`            | `off`   | `off`, `digest` or `both` (see below)               |
| `DIGEST_INTERVAL_SECONDS`| `86400` | Digest flush cadence (minimum `300`)                |
| `DIGEST_MAX_ALERTS`      | `50`    | Maximum alerts rendered per digest message          |
| `RETENTION_DAYS`         | `0`     | Global entry retention window in days; `0` disables pruning (floored at `0`); per-source `retention_days` overrides it |
| `MAINTENANCE_INTERVAL_SECONDS` | `86400` | Background prune cadence (minimum `300`)      |
| `LOG_LEVEL`              | `INFO`  | Log verbosity                                       |

## HTTP API

| Method | Path               | Purpose                                   |
| ------ | ------------------ | ----------------------------------------- |
| GET    | `/healthz`         | Liveness / readiness probe                |
| GET    | `/`                | HTML dashboard                            |
| GET    | `/api/sources`     | List monitored sources (incl. `min_severity`, `notify_min_severity`, `scan_interval_seconds`, `scan_jitter_seconds`, `alert_cooldown_seconds`, `alert_cooldown_jitter_seconds`, `dedupe_window_seconds`, `escalation_window_seconds`, `last_severity_alerts`, `max_alerts_per_scan`, `extra_keywords`, `suppress_keywords`, `openapi_baseline`, `openapi_ignore_paths`, `statuspage_components`, `max_bytes`, `retention_days`, `max_entries`, `truncated`, `failure_count`, `breaker_until`) |
| POST   | `/api/sources`     | Register a source (token protected)       |
| GET    | `/api/sources/:id` | Fetch one source (incl. `min_severity`, `notify_min_severity`, `scan_jitter_seconds`, `alert_cooldown_seconds`, `alert_cooldown_jitter_seconds`, `dedupe_window_seconds`, `escalation_window_seconds`, `last_severity_alerts`, `max_alerts_per_scan`, `extra_keywords`, `suppress_keywords`, `openapi_baseline`, `openapi_ignore_paths`, `statuspage_components`, `max_bytes`, `retention_days`, `max_entries`, `truncated`, `failure_count`, `breaker_until`) |
| GET    | `/api/sources/:id/entries` | List one source's entries, newest first (`?limit=`, `?offset=`) |
| POST   | `/api/sources/:id/reset` | Clear one source's breaker state (token protected) |
| DELETE | `/api/sources/:id` | Remove a source (token protected)         |
| GET    | `/api/alerts`      | List alerts (`?limit=`, `?severity=`)     |
| GET    | `/api/entries`     | List parsed entries (`?source_id=`, `?limit=`, `?offset=`) |
| GET    | `/api/digests`     | List digests, newest first (`?limit=`)    |
| GET    | `/api/digests/:id` | Fetch one digest (`404` when unknown)     |
| GET    | `/api/stats`       | Aggregate counters (incl. `sources_tripped`, `alerts_suppressed` and `entries_pruned`) |
| GET    | `/api/export`      | Full JSON snapshot of sources, entries, alerts and digests (`?limit=`) |
| POST   | `/api/check`       | Run a scan now (token protected)          |
| POST   | `/api/prune`       | Prune entries past their retention window now (token protected); returns `{"entries_pruned": N, "alerts_pruned": M}` |
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
| `notify_min_severity` | no    | Lowest severity that is *notified*: `critical`, `warning` or `info`; omitted or `null` inherits `min_severity`. Alerts below it are stored but never dispatched or digested. Any other value is rejected with a `400` |
| `notify_channels`  | no       | Channels that receive this source's alerts: a list of `console`, `slack`, `email`, `webhook` or `all` (default `all`) |
| `notify_webhook_url` | no     | `http(s)` URL that overrides `WEBHOOK_URL` for this source's alerts and digests; omitted or `null` falls back to the global `WEBHOOK_URL` |
| `scan_interval_seconds` | no  | Positive integer override for this source's polling cadence; omitted keeps the global `POLL_INTERVAL_SECONDS` |
| `scan_jitter_seconds` | no     | Non-negative integer window (seconds) added as a deterministic, id-derived offset to this source's due time so its polls are spread; omitted, `null` or `0` means no jitter |
| `max_bytes`        | no       | Positive integer cap on the response body bytes read and stored for this source; omitted or `null` uses the global `FETCH_MAX_BYTES` |
| `retention_days`   | no       | Positive integer retention window in days for this source's entries; omitted or `null` inherits the global `RETENTION_DAYS`. `0`, negative, non-integer and boolean values are rejected with a `400` |
| `max_entries`      | no       | Positive integer cap on how many entries are kept for this source. After each successful scan the oldest entries beyond the cap are pruned; it composes with `retention_days` (the tighter bound wins) and never deletes alerts or digests. Omitted or `null` keeps the unbounded default; `0`, negative, non-integer and boolean values are rejected with a `400` |
| `max_alerts_per_scan` | no    | Positive integer cap on how many alerts this source may *dispatch* from one scan. New alerts are ordered by severity (`critical` then `warning` then `info`, stable on creation order) and only the top `N` fan out; the rest are stored with `notified = 0`, stay visible in `/api/alerts` and remain digest-eligible, and the held-back count is reported as `alerts_suppressed`. Omitted or `null` means no cap (the default); `0`, negative, non-integer and boolean values are rejected with a `400` |
| `alert_cooldown_seconds` | no | Non-negative integer cooldown window (seconds) that suppresses repeat notifications from this source; omitted, `null` or `0` means no cooldown (the default). Negative, non-integer and boolean values are rejected with a `400` |
| `alert_cooldown_jitter_seconds` | no | Non-negative integer window (seconds) added deterministically to `alert_cooldown_seconds` as `id % (jitter + 1)`, so sources that share a cooldown no longer release their alerts on the same tick. Only meaningful with `alert_cooldown_seconds` set; omitted, `null` or `0` means no jitter (the default). Negative, non-integer and boolean values are rejected with a `400` |
| `dedupe_window_seconds` | no | Non-negative integer window (seconds) that suppresses re-notification of an alert whose normalized title (trimmed, whitespace-collapsed, case-folded) was already dispatched from this source; omitted, `null` or `0` means disabled (the default). Negative, non-integer and boolean values are rejected with a `400` |
| `escalation_window_seconds` | no | Non-negative integer window (seconds) that suppresses re-notification of an alert whose severity was already dispatched from this source within the window; a higher-severity alert always escalates through. Omitted, `null` or `0` means disabled (the default). Negative, non-integer and boolean values are rejected with a `400` |
| `headers`          | no       | JSON object of header name to string value sent on every fetch (merged over the defaults; names normalized case-insensitively) |
| `auth`             | no       | `"user:password"` HTTP Basic credentials; adds `Authorization: Basic <base64>` |
| `extra_keywords`   | no       | List of terms that extend the relevance vocabulary for this source and raise a matching entry to `critical` (trimmed, lower-cased, default `[]`) |
| `suppress_keywords`| no       | List of terms removed from the matched set before classification, so a noisy phrase can be muted per source (trimmed, lower-cased, default `[]`) |
| `openapi_baseline` | no       | `openapi` only. JSON object (a pinned OpenAPI/Swagger spec) that the live document is diffed against instead of the empty spec, so the first scan emits only the changes relative to the baseline; omitted or `null` keeps the empty-spec behaviour. A non-object value is rejected with a `400` naming the field |
| `openapi_ignore_paths` | no   | `openapi` only. List of dotted path or path-prefix strings whose structural changes are dropped before an entry is emitted, for example `paths./internal/health` or `paths./v2/legacy`; exact match or prefix on a `/` boundary. Entries are trimmed, de-duplicated and case-sensitive; default `[]`. A non-list value or a blank entry is rejected with a `400` naming the field |
| `statuspage_components` | no  | `statuspage` only. List of component names or ids (default `[]`). When set, only incidents and scheduled maintenances affecting a listed component are ingested: a `components[].name` is matched case-insensitively and a `components[].id` exactly, and an item that names no component is kept only when the list is empty. Entries are trimmed, de-duplicated and kept case-sensitive. A non-list value or a non-string/blank entry is rejected with a `400` naming the field |

For `html`, `selector` is a CSS-like simple selector (optional tag, `#id`,
`.class`, or a combination). It matches a block when the block or an ancestor
satisfies it, so `#changelog` scopes extraction to that container. For `json`,
`github`, `gitlab` and `openapi`, `selector` is a dotted path such as
`data.releases` or `versions`. A segment that is a non-negative integer indexes
a list (`0` is the first element) and `-1` addresses the last element, so a list
nested at a fixed position can be reached — for example
`data.changelogs.0.entries` against
`{"data": {"changelogs": [{"entries": [...]}]}}`. Invalid segments never raise
and yield no entries, exactly like an unknown key: a numeric segment applied to
an object, a non-integer segment applied to a list, and an out-of-range index
(including `-1` against an empty list) all produce an empty result.

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
surface as `info` entries instead of staying silent. Setting `openapi_baseline`
replaces that empty spec with a pinned spec (see
[Pinning an OpenAPI baseline](#pinning-an-openapi-baseline)), and listing
`openapi_ignore_paths` mutes known-irrelevant changes. Every structural change
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

### Pinning an OpenAPI baseline

A freshly registered `openapi` source with no baseline diffs the live document
against the empty spec, so the whole surface (every path, operation and
parameter) lands as `info` entries — and as `info` alerts at the default
`min_severity`. Operators typically silence that first scan by hand-editing the
database or by raising `min_severity` to `critical`, which then also hides the
`warning`-level changes that matter. `openapi_baseline` fixes this: pin the spec
you already run, and the parser diffs the live document against that pinned spec
instead, so the first scan produces a small, meaningful diff.

```sh
# Pin the production spec as the baseline and mute the internal health/
# deprecated-legacy prefixes. min_severity can stay at its default.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "Acme Public API",
        "url": "https://api.acme.com/openapi.json",
        "kind": "openapi",
        "api_name": "Acme",
        "tracked_versions": ["v2"],
        "openapi_baseline": {
          "openapi": "3.0.0",
          "info": {"version": "2.0.0"},
          "paths": {"/users": {"get": {"responses": {"200": {"description": "ok"}}}}}
        },
        "openapi_ignore_paths": ["paths./internal/health", "paths./v2/legacy"]
      }'
```

With that source registered, the next scan emits only the structural changes
between the pinned `2.0.0` spec and the live document, minus any change under
`paths./internal/health` or `paths./v2/legacy` (prefix matching also covers
`paths./v2/legacy/sub`). The dashboard's `OpenAPI` column shows a compact
`baseline · ignore 2` indicator and never renders the pinned payload; `GET
/api/sources` and `GET /api/sources/:id` return both fields, defaulting to
`null` and `[]`. A source with neither field set scans identically to before.

### Filtering status-page components

A statuspage.io page often covers many unrelated components — a vendor's
marketing site, its billing system and the API you actually depend on all share
one incident feed — so ingesting every incident and scheduled maintenance floods
the alert stream with changes that cannot affect your integration. The optional
`statuspage_components` list on a `statuspage` source narrows ingestion to the
components you care about:

```sh
# Only ingest incidents and maintenances affecting the API component
# (by name, case-insensitively) or the component with id "abc123" (exactly).
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Status","url":"https://status.acme.com/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Acme","tracked_versions":["v1"],
       "statuspage_components":["API","abc123"]}'
```

A `components[].name` is matched case-insensitively (so `api`, `Api` and `API`
all match) and a `components[].id` exactly (so `abc123` matches but `ABC123`
does not). An item that names no component is kept only when the list is empty,
so a non-empty filter never silently keeps an unrelated incident. Entries are
trimmed, de-duplicated and kept case-sensitive; a non-list value or a
non-string/blank entry is rejected with a `400` naming `statuspage_components`.
Omitting the field keeps the previous behaviour byte-for-byte: every incident
and maintenance is ingested exactly as before. The list is returned by
`GET /api/sources` and `GET /api/sources/:id`, old databases are migrated to read
back `[]`, and the dashboard's `OpenAPI` extra column shows a compact
`components N` indicator (`–` when unset).

`min_severity` controls the per-source alert floor. The relevance engine still
classifies and stores every matched entry, but only entries whose severity ranks
at or above the floor become alerts. The ladder is `critical` > `warning` >
`info`, so a source with `min_severity: "warning"` ignores `info`-level release
notes but still alerts on `warning` and `critical` deprecations. Entries below
the floor remain in the history (`/api/entries`) and are de-duplicated normally.

`notify_min_severity` adds a second, notification-only floor. It is `critical`,
`warning` or `info` (case-insensitive, trimmed) and defaults to inheriting
`min_severity` when omitted or `null`. An alert that clears `min_severity` but
ranks below the effective notification floor (`notify_min_severity` if set,
otherwise `min_severity`) is still created and stored, appears in
`GET /api/alerts`, `GET /api/entries` and `GET /api/export`, and keeps
`notified = 0`, but is never fanned out by immediate dispatch and is never
claimed by a digest flush. This lets an operator record `info` and `warning`
alerts for history while only ever notifying on `critical`. Held-back alerts are
counted as `alerts_suppressed` (composing with the cooldown: an alert must clear
both filters to be delivered), the dashboard shows a compact
`notify: <severity>` indicator next to the `Min severity` badge (or `–` when
inheriting), and any value outside `critical`/`warning`/`info` is rejected with a
`400` naming `notify_min_severity`. Omitting the field on every source is
byte-for-byte equivalent to the pre-feature behaviour.

`alert_cooldown_seconds` coalesces repeat notifications from one noisy source.
It is a non-negative integer number of seconds; omitting it, sending `null` or
sending `0` means no cooldown and is byte-for-byte equivalent to the
pre-feature behaviour. When set, an alert is only fanned out if no alert from
the same source was dispatched within the previous `alert_cooldown_seconds`; the
last dispatch time is persisted on the source row so the window survives a
restart. The cooldown suppresses *notification only*: the underlying entry and
its alert are still stored, keep `notified = 0`, appear in `GET /api/alerts` and
`GET /api/entries`, and remain eligible for the next digest flush (so a digest
still summarises them). This mirrors the `min_severity` split between
persistence and alerting. The number of held-back alerts is reported as
`alerts_suppressed` on the `POST /api/check` result and aggregated into
`GET /api/stats`, and the dashboard shows a `suppressed` card plus a compact
`cooldown Ns` indicator in the source's `Interval` column. A negative,
non-integer, boolean or string value is rejected with a `400` naming
`alert_cooldown_seconds`.

```sh
# A noisy changelog that repeats the same deprecation: keep every entry and
# alert once, then coalesce repeats for an hour.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Noisy changelog","url":"https://noisy.example/changelog.json",
       "kind":"json","api_name":"Noisy","tracked_versions":["v2"],
       "alert_cooldown_seconds":3600}'
```

`alert_cooldown_jitter_seconds` spreads the effective alert-cooldown window so a
large registry does not release every source's coalesced alerts on the same
tick. It is a non-negative integer number of seconds; omitting it, sending
`null` or sending `0` means no jitter and is byte-for-byte equivalent to the
pre-feature engine. When set (and the source also has an
`alert_cooldown_seconds`), the effective cooldown becomes
`alert_cooldown_seconds + (source.id % (alert_cooldown_jitter_seconds + 1))`.
The offset is derived from the source id — the same `id % (jitter + 1)`
derivation as `scan_jitter_seconds` — and is never random, so the same
`(id, jitter)` pair always yields the same offset and dispatch stays
reproducible and testable. Like the base cooldown, jitter is *notification
only*: held-back alerts are still stored with `notified = 0`, appear in
`GET /api/entries`, `GET /api/alerts` and `GET /api/export`, stay eligible for a
later digest, and are counted as `alerts_suppressed` in `GET /api/stats`; the
`last_alert_at` dispatch time is still persisted, so a restart does not reset
the jittered window. It composes with the notification floor, the title-dedupe
window and the severity escalation window — an alert must clear all of them to
be delivered. A source with no `alert_cooldown_seconds` (unset or `0`) is
unaffected by any jitter value. A negative integer, a non-integer, a boolean or
a non-numeric value is rejected with a `400` naming
`alert_cooldown_jitter_seconds`. The dashboard appends a compact `+0-Ns`
indicator after `cooldown Ns` when the jitter is set (for example
`cooldown 300s +0-60s`) and leaves the existing `cooldown Ns` / `dedupe Ns` /
`escalate Ns` output unchanged when it is unset.

```sh
# 50 sources share a one-hour cooldown: spread their release over an extra
# deterministic 0-60s window per source id instead of all firing at once.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Shared upstream","url":"https://vendor.example/changelog.json",
       "kind":"json","api_name":"Vendor","tracked_versions":["v2"],
       "alert_cooldown_seconds":3600,"alert_cooldown_jitter_seconds":60}'
```

`dedupe_window_seconds` coalesces re-notifications of the *same alert title*
from one source. It is a non-negative integer number of seconds; omitting it,
sending `null` or sending `0` disables it and is byte-for-byte equivalent to the
pre-feature behaviour. Where the cooldown silences the whole source for its
window, the dedupe window is keyed on the alert's normalized title — trimmed,
internal whitespace collapsed and case-folded — so `"v1 deprecated"` and
`"V1   Deprecated"` share a window while a genuinely new title is still
delivered inside another title's window. A title is held back when it was
already dispatched from the same source within the previous
`dedupe_window_seconds`, which catches a vendor that re-posts the same
announcement with a slightly changed body or a new `published` date. Like the
cooldown it suppresses *notification only*: the entry and its alert are still
stored with `notified = 0`, appear in `GET /api/entries`, `GET /api/alerts` and
`GET /api/export`, and remain eligible for the next digest flush. The per-title
dispatch times are persisted in the source's `last_title_alerts` map so a restart
does not reset the window, and the held-back alerts are counted as
`alerts_suppressed` on the `POST /api/check` result and in `GET /api/stats`. The
dashboard shows a compact `dedupe Ns` indicator in the `Interval` column, or `–`
when unset. A negative, non-integer, boolean or string value is rejected with a
`400` naming `dedupe_window_seconds`. It composes with the notification floor and
the cooldown: an alert must clear all three to be delivered.

```sh
# A statuspage that re-posts the same incident headline with a new body every
# few minutes: keep every entry, alert once, then coalesce repeats for 30 min.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Payments status","url":"https://status.payments.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Payments","tracked_versions":["v1"],
       "dedupe_window_seconds":1800}'
```

`escalation_window_seconds` coalesces repeated notifications of the *same
severity* from one source. It is a non-negative integer number of seconds;
omitting it, sending `null` or sending `0` disables it and is byte-for-byte
equivalent to the pre-feature behaviour. When set, an alert at or above the
source's effective notification floor (`notify_min_severity` if set, otherwise
`min_severity`) is only dispatched if no alert of the same severity from the same
source was dispatched within the previous `escalation_window_seconds`; a
higher-severity alert always escalates through, so after a `warning` a `critical`
is still delivered, but a repeat `critical` inside the window is held back. It
closes the gap left by the cooldown and the title window: a vendor that
re-announces the same-severity change every few days with a drifting title and
body is no longer paged on every re-announcement. Like the other filters it
suppresses *notification only*: the entry and its alert are still stored with
`notified = 0`, appear in `GET /api/entries`, `GET /api/alerts` and
`GET /api/export`, and remain eligible for the next digest flush. The per-severity
dispatch times are persisted in the source's `last_severity_alerts` map
(merged last-write-wins) so a restart does not reset the window, and the held-back
alerts are counted as `alerts_suppressed` on the `POST /api/check` result and in
`GET /api/stats`. The dashboard shows a compact `escalate Ns` indicator in the
`Interval` column, or `–` when unset. A negative, non-integer, boolean or string
value is rejected with a `400` naming `escalation_window_seconds`. It composes
with the notification floor, the cooldown and the title-dedupe window: an alert
must clear all four to be delivered.

```sh
# A source that re-announces a critical change every few days: record every
# entry, page at most once a day per severity, and always let a higher severity
# escalate through.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Vendor SDK","url":"https://vendor.example/changelog.json",
       "kind":"json","api_name":"Vendor","tracked_versions":["v2"],
       "escalation_window_seconds":86400}'
```

`max_alerts_per_scan` bounds how many alerts one scan may *fan out*. It is a
positive integer; omitting it or sending `null` means no cap and is
byte-for-byte equivalent to the pre-feature behaviour. When set, the alerts
created in a scan are ordered by severity (`critical` > `warning` > `info`,
stable on creation order) and only the first `N` are handed to dispatch; the
rest are held back from that scan's fan-out. The held-back alerts are still
stored with `notified = 0`, appear in `GET /api/alerts`, `GET /api/entries` and
`GET /api/export`, and remain eligible for a later digest flush, so the cap is
*notification only* — the audit trail is never shortened. It catches the case
none of the time-based filters do: a source that emits dozens of genuinely
distinct alerts in a single scan. The number of held-back alerts is reported as
`alerts_suppressed` on the `POST /api/check` result and aggregated into
`GET /api/stats`, and the dashboard shows a compact `fanout N` indicator in the
`Interval` column (or `fanout –` when unset). It composes with the notification
floor, cooldown, dedupe and escalation filters; in `DIGEST_MODE=digest` no
immediate dispatch happens, so the cap is deferred to dispatch time and does not
double-count the notification-floor accounting. A `0`, negative, non-integer or
boolean value is rejected with a `400` naming `max_alerts_per_scan`.

```sh
# A vendor that occasionally dumps dozens of breaking changes at once: keep every
# alert but fan out at most 3 per scan, highest severity first.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Batch vendor","url":"https://vendor.example/changelog.json",
       "kind":"json","api_name":"Vendor","tracked_versions":["v2"],
       "max_alerts_per_scan":3}'
```

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

`scan_jitter_seconds` spreads a source's polls across a window so a large
registry does not fire every source on the same tick against a shared upstream
(statuspage, GitHub/GitLab, vendor changelog). It is a non-negative integer
number of seconds; omitting it (or sending `null` or `0`) means no jitter and is
byte-for-byte equivalent to the pre-feature scheduler. When set, the scheduler
adds a deterministic offset in `[0, scan_jitter_seconds]`, computed as
`source.id % (scan_jitter_seconds + 1)`, to the source's next due time, so the
effective due time is `last_scan + effective_interval + offset` — composing with
both `scan_interval_seconds` and the global `POLL_INTERVAL_SECONDS`. The offset
is stable for a given source and jitter (never random), so schedules stay
reproducible and testable. The scheduler wakes at the smallest effective due
time among the enabled, non-tripped sources and does not busy-loop while a
jittered source waits. A negative integer, a non-integer, a boolean or a
non-numeric value is rejected with a `400` naming `scan_jitter_seconds`. A
manual `POST /api/check` ignores jitter (and intervals) and scans every enabled
source immediately.

```sh
# Poll every 15 minutes, but spread N sources over a 5-minute window so they
# do not all hit the shared statuspage on the same tick.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Payments API","url":"https://status.payments.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Payments","tracked_versions":["v1"],
       "scan_interval_seconds":900,"scan_jitter_seconds":300}'
```

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
the optional `?limit=` query parameter (default `100`). The optional `?offset=`
query parameter (a non-negative integer, default `0`) skips that many newest
entries before `limit` is applied, so the history can be paged. An unknown `:id`
returns `404 {"error": "source not found"}`, while a non-integer `:id` or
`?limit=` returns `400 {"error": "invalid id"}` or `{"error": "invalid limit"}`
respectively and an invalid `?offset=` (non-integer, negative or boolean)
returns `400 {"error": "invalid offset"}`. The route matches the `/entries`
suffix, so `GET` and `DELETE /api/sources/:id` are unchanged.

```sh
# The stored history for source 1, newest first, at most 20 entries.
curl "http://localhost:8080/api/sources/1/entries?limit=20"

# Page further back: skip the 20 newest, return the next 20.
curl "http://localhost:8080/api/sources/1/entries?limit=20&offset=20"
```

### Paging the entry feed

`GET /api/entries` accepts the same `?limit=` (default `100`) and `?offset=`
(default `0`) pair as the per-source route, plus `?source_id=` to filter to one
dependency. Omitting `offset` returns exactly the same page as before pagination
existed. Combine `limit` and `offset` to walk the whole history of a
long-running deployment, which may hold thousands of entries:

```sh
# Page 1: the 100 newest entries.
curl "http://localhost:8080/api/entries?limit=100"

# Page 2: the next 100 newest entries.
curl "http://localhost:8080/api/entries?limit=100&offset=100"

# Page 3, and an offset past the end returns an empty list with status 200.
curl "http://localhost:8080/api/entries?limit=100&offset=200"
curl "http://localhost:8080/api/entries?limit=100&offset=100000"
# -> {"entries": []}

# An invalid offset is rejected with 400 naming the parameter.
curl "http://localhost:8080/api/entries?offset=-1"
# -> {"error": "invalid offset"}
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

## Retention

A long-running deployment should not grow its SQLite database without bound, so
entries and the alerts that reference them can be pruned once they fall outside a
retention window. Set the deployment-wide window with `RETENTION_DAYS` (default
`0`, meaning disabled and floored at `0`) and override it per source with the
optional positive integer `retention_days`.

The effective window for a source is its own `retention_days` when set, otherwise
the global `RETENTION_DAYS`. A window of `0` disables pruning for that source.
The background scheduler runs a prune pass every `MAINTENANCE_INTERVAL_SECONDS`
(default `86400`, minimum `300`); operators can also trigger one immediately:

```sh
# Prune now (token protected when API_TOKEN is set).
curl -X POST http://localhost:8080/api/prune \
  -H "Authorization: Bearer $API_TOKEN"
# -> {"entries_pruned": 12, "alerts_pruned": 9}
```

Pruning deletes entries whose `created_at` is older than the effective window
and deletes the alerts whose `entry_id` was pruned. **It never deletes rows from
`sources` or `digests`**, so the source registry, delivered digests and the
aggregate `stats` survive a prune; only the per-source entry history (and the
alerts derived from it) is bounded. An entry whose retention window is unset and
whose global default is `0` is always kept. Databases created before this feature
are migrated automatically and read back with `retention_days` as `null`.

```sh
# Keep the global history bounded at 90 days, but trim a chatty dependency to 7.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Chatty","url":"https://chatty.example/changelog.json",
       "kind":"json","retention_days":7}'
```

The dashboard's `Retention` column shows a source's `retention_days` as a
compact `Nd` label (for example `7d`) or `–` when it is unset and inherits the
global default; it never renders headers or credentials.

### Entry-count cap

Retention bounds history by age; an optional positive `max_entries` bounds it by
count instead. After every successful scan the engine keeps only the newest
`max_entries` entries for that source, so one chatty dependency cannot grow the
SQLite file without bound inside its retention window. The two bounds compose —
a source with `retention_days = D` and `max_entries = N` ends up with at most
`min(N, entries within D days)` — and the tighter one wins. **The cap never
deletes alerts or digests**, so an alert whose entry was pruned still appears in
`/api/alerts` (and `/api/export`) with its stored title and link, and
`/api/digests` is unaffected. Omitting the field (or sending `null`) keeps the
unbounded default, so existing sources and databases are unchanged. The total
removed by retention and by the cap since process start is reported as
`entries_pruned` in `GET /api/stats`, and the dashboard shows a compact
`cap N` label next to the retention window (`cap –` when the source is
unbounded).

```sh
# Keep at most the 200 newest entries for one chatty dependency.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Chatty","url":"https://chatty.example/changelog.json",
       "kind":"json","max_entries":200}'
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
`config.digestIntervalSeconds` and `config.digestMaxAlerts` in `values.yaml`;
the circuit breaker through `config.breakerThreshold` and
`config.breakerCooldownSeconds`; the fetch size cap through
`config.fetchMaxBytes` (`FETCH_MAX_BYTES`); and entry retention through
`config.retentionDays` (`RETENTION_DAYS`) and
`config.maintenanceIntervalSeconds` (`MAINTENANCE_INTERVAL_SECONDS`).

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
