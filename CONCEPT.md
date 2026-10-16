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
  An operator can replace that empty spec with a pinned `openapi_baseline` (a
  JSON spec object, default `null`) so a freshly registered source diffs the
  live document against the pinned spec and the first scan emits only the
  structural changes relative to it rather than the whole surface. A second
  optional field, `openapi_ignore_paths` (a list of dotted path or path-prefix
  strings, default `[]`), drops structural changes whose affected path matches
  an entry — exact match or prefix on a `/` boundary, so `paths./v2/legacy` also
  covers `paths./v2/legacy/sub` but not `paths./v2/legacyv2`. Both controls are
  inert when unset, so a source with neither field set behaves exactly as
  before. The severity is derived from the kind of change:
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
- **Array indices in dotted selectors:** the `json`, `github`, `gitlab` and
  `openapi` parsers accept numeric segments inside that dotted path, so a list
  nested at a fixed position — for example
  `{"data": {"changelogs": [{"entries": [...]}]}}` — can be addressed directly
  with `data.changelogs.0.entries`. `0`, `1`, … index from the front and `-1`
  addresses the last element. The walk lives in one shared helper, so all four
  parsers get it and none grows its own copy. Invalid input degrades exactly like
  an unknown key instead of raising: a numeric segment applied to an object, a
  non-integer segment applied to a list, and an out-of-range (including
  negative-into-empty) index each leave the walk with no value, so the parser
  returns no entries and the source scans cleanly.
- **Per-source status-page component filter:** a `statuspage` source may carry a
  `statuspage_components` list (component names or ids, default `[]`). When set,
  only incidents and scheduled maintenances whose affected component matches an
  entry are emitted: a `components[].name` is matched case-insensitively and a
  `components[].id` exactly, and an item that names no component is kept only
  when the list is empty, so nothing is silently dropped by default. When the
  list is unset the parser is byte-for-byte identical to before this control, so
  a page that covers many unrelated components (a vendor's marketing site,
  billing, and the API a team actually depends on) no longer drowns the alert
  stream in incidents that cannot affect the integration. Values are trimmed,
  de-duplicated and kept case-sensitive on registration and stored JSON-encoded
  in the `sources` table.
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
- **Per-source notification floor:** every source optionally carries a
  `notify_min_severity` (`critical`, `warning` or `info`, default `None`, which
  means "inherit `min_severity`"). It is a second, *notification-only* floor:
  entries that clear `min_severity` are still classified and stored and still
  produce an alert, but an alert whose severity ranks below the
  effective notification floor (`notify_min_severity or min_severity`) is never
  fanned out by `Engine.dispatch` and is never claimed by a digest flush. It
  stays in the store with `notified = 0` — visible through `/api/entries`,
  `/api/alerts` and `/api/export` and eligible for a later digest once the floor
  is lowered — so an operator can "record `info` for history, but only *notify*
  on `critical`" without dropping the lower-severity rows from the store. The
  number of alerts held back is counted as `alerts_suppressed` (the same field
  the cooldown uses) and the dashboard shows a compact `notify: <severity>`
  indicator, or `–` when the source inherits `min_severity`.
- **Per-source alert cooldown:** every source optionally carries an
  `alert_cooldown_seconds` (a non-negative integer, default `None` / `0`, which
  means no cooldown and is byte-for-byte equivalent to the pre-feature
  behaviour). When set, an alert is only fanned out if no alert from the same
  source was dispatched within the previous `alert_cooldown_seconds`. The
  cooldown suppresses *notification only*: the entry and its alert are still
  stored, keep `notified = 0` and remain eligible for the next digest flush, so
  history, `/api/entries`, `/api/alerts`, de-duplication and digests are
  unchanged — the same separation of persistence from alerting that
  `min_severity` already uses. The last dispatch time is persisted on the source
  row (`last_alert_at`) so a restart does not reset the window, and the number of
  alerts held back is counted on the scan result as `alerts_suppressed`,
  aggregated into `GET /api/stats`. A noisy dependency whose changelog
  re-announces the same deprecation, or emits many near-identical entries in a
  short window, therefore no longer floods Slack, e-mail or the webhook channel
  with duplicates.
- **Per-source alert-cooldown jitter:** every source optionally carries an
  `alert_cooldown_jitter_seconds` (a non-negative integer, default `None` / `0`,
  which means no jitter and is byte-for-byte equivalent to the pre-feature
  behaviour). It spreads the effective alert-cooldown window by a deterministic,
  source-id-derived offset so a large registry in which many sources share the
  same `alert_cooldown_seconds` no longer releases their coalesced alerts on the
  same tick. The effective cooldown becomes
  `alert_cooldown_seconds + (source.id % (alert_cooldown_jitter_seconds + 1))`
  when the jitter is set — the same `id % (jitter + 1)` derivation as
  `scan_jitter_seconds`, with no randomness — and is byte-for-byte identical to
  today's behaviour when the jitter is omitted, `null` or `0`. It composes with
  `alert_cooldown_seconds` and the other notification filters (the notification
  floor, the title-dedupe window and the severity escalation window): an alert
  must clear all of them, applied to the jittered cooldown, to be delivered. A
  source with no `alert_cooldown_seconds` (or `0`) is unaffected regardless of
  the jitter value. Like the existing cooldown it is *notification-only*: the
  entry and its alert are still stored, keep `notified = 0`, remain visible via
  `/api/entries`, `/api/alerts` and `/api/export` and stay eligible for a later
  digest, and `last_alert_at` is still persisted so a restart does not reset the
  window. Held-back alerts are counted as `alerts_suppressed`, and the dashboard
  shows a compact indicator only when the jitter is set (for example
  `cooldown 300s +0-60s`), never rendering any secret value.
- **Per-source title dedupe window:** every source optionally carries a
  `dedupe_window_seconds` (a non-negative integer, default `None` / `0`, which
  means disabled and is byte-for-byte equivalent to the pre-feature behaviour).
  Where `alert_cooldown_seconds` measures the time since the last dispatch *of
  any alert* from the source, the dedupe window measures the time since the last
  dispatch *of the same normalized title*, so a vendor that re-posts the same
  deprecation announcement with a slightly changed body or a new `published`
  date no longer produces a second Slack/e-mail/webhook message. Titles are
  normalized (trimmed, internal whitespace collapsed, case-folded) before the
  comparison, so `"v1 deprecated"` and `"V1   Deprecated"` share one window. The
  window is *notification-only*: entries and alerts are still stored, keep
  `notified = 0`, remain visible through `/api/entries`, `/api/alerts` and
  `/api/export`, and stay eligible for a later digest once the window elapses.
  The last dispatch time per normalized title is persisted on the source row
  (`last_title_alerts`) so a restart does not reset the window, and the number
  of alerts held back is counted as `alerts_suppressed` (the same field the
  cooldown uses) and aggregated into `GET /api/stats`. The dashboard shows a
  compact `dedupe Ns` indicator next to the `cooldown Ns` indicator, or `–` when
  the source has no dedupe window.
- **Per-source severity escalation window:** every source optionally carries an
  `escalation_window_seconds` (a non-negative integer, default `None` / `0`,
  which means disabled and is byte-for-byte equivalent to the pre-feature
  behaviour). When set, an alert whose severity is at or above the source's
  effective notification floor (`notify_min_severity or min_severity`) is only
  dispatched if no alert of the *same severity* from the same source was
  dispatched within the previous `escalation_window_seconds`; a higher-severity
  alert always escalates through. This closes the gap the cooldown and the
  title-dedupe window leave open: a source that re-announces a `critical` change
  every few days — with a drifting title and body, so content-hash de-duplication
  and the title window both miss it — no longer pages the on-call engineer each
  time, while a genuinely new `warning` still gets through. Like the other
  filters it suppresses *notification only*: entries and alerts are still
  stored, keep `notified = 0`, remain visible via `/api/entries`, `/api/alerts`
  and `/api/export`, and stay eligible for a later digest once the window
  elapses. It composes with `alert_cooldown_seconds`, `dedupe_window_seconds` and
  `notify_min_severity` — an alert must clear all of them to be delivered — and
  the per-severity dispatch times are persisted on the source row
  (`last_severity_alerts`) so a restart does not reset the window. The number of
  alerts held back is counted as `alerts_suppressed` (the same field the cooldown
  and dedupe window use) and aggregated into `GET /api/stats`. The dashboard
  shows a compact `escalate Ns` indicator next to `cooldown Ns` and `dedupe Ns`,
  or `–` when unset.
- **Per-source per-scan alert fan-out cap:** every source optionally carries a
  positive integer `max_alerts_per_scan` (default `None`, i.e. no cap). When
  set, at most that many alerts are actually fanned out from one scan; a source
  that suddenly emits dozens of genuinely distinct entries (a vendor dumping a
  batch of breaking changes, a statuspage flapping, an OpenAPI diff that
  surfaces many structural changes at once) can no longer flood Slack, e-mail
  and the webhook channel in a single tick. The remainder are still stored and
  keep `notified = 0`, remain visible through `/api/entries`, `/api/alerts` and
  `/api/export` and stay eligible for a later digest flush. Alerts are
  prioritised by severity (`critical`, then `warning`, then `info`) with a
  stable sort so equal-severity alerts keep their creation order and the most
  dangerous changes always get through. The number held back is counted as
  `alerts_suppressed` (the same field the cooldown, dedupe, escalation and
  notification-floor filters use) and aggregated into `GET /api/stats`, and the
  dashboard shows a compact `fanout N` indicator (or `fanout –` when unset).
  Like the other notification filters it is *notification-only* — entries and
  alerts are always stored — and a source with no cap behaves exactly as before.
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
  `notify_min_severity` (text or `NULL`, where `NULL` means inherit
  `min_severity`),
  `scan_interval_seconds`, `scan_jitter_seconds` (integer or `NULL`, where
  `NULL`/`0` means no jitter), `max_bytes` (integer or `NULL`),
  `retention_days` (integer or `NULL`), `max_entries` (integer or `NULL`),
  `max_alerts_per_scan` (integer or `NULL`, where `NULL` means no per-scan cap),
  `alert_cooldown_seconds` (integer or
  `NULL`), `alert_cooldown_jitter_seconds` (integer or `NULL`, where
  `NULL`/`0` means no jitter), `last_alert_at` (text, the last dispatch time that
  anchors the cooldown),   `dedupe_window_seconds` (integer or `NULL`), `last_title_alerts`
  (JSON text, the map of normalized title -> ISO-8601 last dispatch time that
  anchors the per-title dedupe window, read back as `{}` when unset),
  `escalation_window_seconds` (integer or `NULL`) and `last_severity_alerts`
  (JSON text, the map of severity -> ISO-8601 last dispatch time that anchors
  the per-severity escalation window, read back as `{}` when unset), and
  `alerts_suppressed` (integer, default `0`), `headers` (JSON
  text), `auth` (text),
  `extra_keywords` and `suppress_keywords` (JSON text),
  `openapi_baseline` (JSON text or `NULL`, where `NULL` means "diff against the
  empty spec") and `openapi_ignore_paths` (JSON text, read back as `[]` when
  unset), `statuspage_components` (JSON text, read back as `[]` when unset),
  `failure_count` (integer, default `0`), `breaker_until` (text) and
  `truncated` (integer, default `0`), and a
  lightweight migration adds each column to databases created before it existed,
  leaving old rows at `NULL`/empty/`0` (read back as `{}` / `None` / `[]` /
  `0` / `None` / `false`).
- **Per-source history API + pagination:** each source's recorded entries are
  browsable newest-first through `GET /api/sources/:id/entries` (optional
  `?limit=`, default `100`, and `?offset=`, default `0`), so the stored history
  of one dependency can be inspected without filtering the global `/api/entries`
  feed. The same optional `?offset=` (a non-negative integer, default `0`) is
  accepted by the global `GET /api/entries`, composing with `?limit=`: `offset`
  skips that many newest entries first, then `limit` caps the page, so
  `GET /api/entries?limit=100&offset=100` walks the next page of a long-running
  deployment's history. Omitting `offset` is byte-for-byte unchanged from before
  pagination existed. An unknown source id returns `404`; a non-integer id or
  limit, or an invalid `offset` (non-integer, negative or boolean), returns
  `400` naming the offending parameter; the `/entries` suffix leaves `GET` and
  `DELETE /api/sources/:id` unchanged.
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
- **Bounded history (retention):** a configurable per-source entry retention
  policy bounds how long parsed entries (and the alerts derived from them)
  survive in SQLite, so a long-running deployment does not grow the database
  without limit. A global `RETENTION_DAYS` default (also readable as
  `retention_days` per source) sets the window; the scheduler prunes on a
  configurable maintenance cadence and `POST /api/prune` prunes on demand. The
  `sources` registry and `digests` are never pruned, so the catalog and the
  aggregate history stay intact.
- **Per-source entry-count cap:** every source optionally carries a positive
  integer `max_entries`. After each successful scan the engine deletes the oldest
  entries for that source until at most `max_entries` remain (newest-first), so a
  single chatty dependency cannot grow the SQLite store without bound even inside
  its retention window. The cap is count-based and composes with the age-based
  `retention_days` window: the tighter of the two bounds wins, so a source with
  both keeps at most `min(N, entries within D days)`. Unlike the retention pass,
  the cap never deletes alerts or digests: an alert whose entry was capped away
  still appears in `GET /api/alerts` (and `/api/export`) with its stored `title`
  and `link`, and `GET /api/digests` is untouched. Omitting the field (or
  sending `null`) keeps today's unbounded behaviour byte-for-byte, so existing
  databases and sources are unchanged. The number of entries dropped by the cap
  is surfaced, together with the retention count, as `entries_pruned` in
  `GET /api/stats`.
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
| GET    | `/api/sources`    | List monitored sources (incl. `min_severity`, `notify_min_severity`, `notify_channels`, `notify_webhook_url`, `scan_interval_seconds`, `scan_jitter_seconds`, `max_bytes`, `retention_days`, `max_entries`, `max_alerts_per_scan`, `alert_cooldown_seconds`, `alert_cooldown_jitter_seconds`, `dedupe_window_seconds`, `escalation_window_seconds`, `last_severity_alerts`, `headers`, `auth`, `extra_keywords`, `suppress_keywords`, `openapi_baseline`, `openapi_ignore_paths`, `statuspage_components`, `failure_count`, `breaker_until`, `truncated`) |
| POST   | `/api/sources`    | Register a source, incl. optional `min_severity`, `notify_min_severity`, `notify_channels`, `notify_webhook_url`, `scan_interval_seconds`, `scan_jitter_seconds`, `max_bytes`, `retention_days`, `max_entries`, `max_alerts_per_scan`, `alert_cooldown_seconds`, `alert_cooldown_jitter_seconds`, `dedupe_window_seconds`, `escalation_window_seconds`, `headers`, `auth`, `extra_keywords`, `suppress_keywords`, `openapi_baseline`, `openapi_ignore_paths` and `statuspage_components` (token protected) |
| GET    | `/api/sources/:id`| Fetch one source (incl. `notify_min_severity`, `alert_cooldown_seconds`, `alert_cooldown_jitter_seconds`, `dedupe_window_seconds`, `escalation_window_seconds`, `last_severity_alerts`, `scan_jitter_seconds`, `retention_days`, `max_entries`, `max_alerts_per_scan`, `openapi_baseline`, `openapi_ignore_paths`, `statuspage_components` and breaker fields) |
| GET    | `/api/sources/:id/entries` | List one source's entries, newest first (`?limit=`, `?offset=`) |
| POST   | `/api/sources/:id/reset` | Clear one source's `failure_count` and `breaker_until` (token protected) |
| DELETE | `/api/sources/:id`| Remove a source (token protected)         |
| GET    | `/api/alerts`     | List alerts, newest first                 |
| GET    | `/api/entries`    | List parsed changelog entries (`?limit=`, `?offset=`, `?source_id=`) |
| GET    | `/api/digests`    | List digests, newest first (`?limit=`)    |
| GET    | `/api/digests/:id`| Fetch one digest (`404` when unknown)     |
| POST   | `/api/digest/flush`| Build and deliver a digest (token protected) |
| POST   | `/api/check`      | Run a scan now (token protected)          |
| POST   | `/api/prune`      | Prune entries past their retention window now (token protected); returns `entries_pruned` and `alerts_pruned` |
| GET    | `/api/stats`      | Aggregate counters (incl. `sources_tripped`, `alerts_suppressed` and `entries_pruned`) |
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

## OpenAPI baseline and ignore paths

A freshly registered `openapi` source normally diffs the live document against
the empty spec, so every path, operation and parameter shows up as an `info`
entry (and, at the default `min_severity`, an `info` alert). That whole-surface
baseline is useful once, but it is noise on every subsequent scan of a
well-established API. Two optional per-source fields turn that first scan into a
small, meaningful diff without lowering `min_severity`:

- **`openapi_baseline`** — a JSON object (a pinned OpenAPI/Swagger spec),
  default `null`. When set, the parser diffs the live document against this
  baseline instead of the empty spec, so only the structural changes relative to
  the pinned spec are emitted. It is applied to the single-spec (selector-less)
  case; a source that pins a baseline *and* points at a list of versions diffs
  the baseline against the first version and then the versions pairwise. The
  value must be a JSON object or `null`; a list, string, number or boolean is
  rejected on registration with
  `400 {"error": "'openapi_baseline' must be a JSON object or null"}`.
- **`openapi_ignore_paths`** — a list of dotted path or path-prefix strings,
  default `[]`. A structural change whose affected path matches an entry is
  dropped before the entry is emitted, so known-irrelevant surface (for example
  `paths./internal/health` or `paths./v2/legacy`) stays quiet without muting the
  rest of the spec. The affected path is written `paths.<path>`, so a change on
  `/v2/legacy` becomes `paths./v2/legacy`; matching is by exact equality or by
  prefix on a `/` boundary, so `paths./v2/legacy` also covers
  `paths./v2/legacy/sub` but not `paths./v2/legacyv2`. Entries are trimmed,
  de-duplicated and kept case-sensitive (OpenAPI paths are case-sensitive);
  a non-list value or a blank entry is rejected with
  `400 {"error": "'openapi_ignore_paths' must be a list of strings"}`.

Both fields default to "off" (`null` / `[]`), and a source with neither set is
byte-for-byte identical to the pre-feature behaviour: the same entries,
severities and content hashes as before. They are stored on the `sources` table,
returned by `GET /api/sources` and `GET /api/sources/:id`, and shown as a compact
`baseline` / `ignore N` indicator in the dashboard's `OpenAPI` column (the
pinned payload itself is never rendered).

```sh
# Pin the current production spec as the diff baseline for a new source and
# mute the internal health/legacy prefixes so only real public changes alert.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Public API","url":"https://api.acme.com/openapi.json",
       "kind":"openapi","api_name":"Acme","tracked_versions":["v2"],
       "openapi_baseline":{"openapi":"3.0.0","info":{"version":"2.0.0"},
                           "paths":{"/users":{"get":{}}}},
       "openapi_ignore_paths":["paths./internal/health","paths./v2/legacy"]}'

# The first scan now reports only what changed since 2.0.0, minus the ignored
# prefixes; everything else is silent, so `min_severity` can stay at `info`.
```

## Status-page component filter

A statuspage.io page often covers many unrelated components: a vendor's
marketing site, its billing system and the API a team actually depends on all
share one incident feed. Ingesting every incident and scheduled maintenance
means the team is alerted for changes that cannot affect its integration, which
erodes trust and pushes operators to mute the source entirely — losing the
signal they wanted. The optional per-source `statuspage_components` list
narrows a `statuspage` source to the components that matter.

- **Field:** `statuspage_components` is an optional list of component names or
  ids (default `[]`). Entries are trimmed, de-duplicated in order and kept
  case-sensitive on registration; a non-list value or a non-string / blank entry
  is rejected with `400 {"error": "'statuspage_components' must be a list of
  strings"}`.
- **Matching:** a filter entry matches an incident's or maintenance's affected
  component when it equals a `components[].name` case-insensitively (so `api`,
  `Api` and `API` all match) *or* a `components[].id` exactly (so `abc123`
  matches `id: "abc123"` but not `id: "ABC123"`). An item is kept when any of
  its components matches any entry.
- **Empty filter is inert:** a source registered without the field behaves
  byte-for-byte as before, including items that name no component at all. When
  the list is non-empty, an item with an empty or missing `components` list is
  dropped, so nothing is silently kept by default.
- **Persistence:** the list is stored JSON-encoded in the `sources` table and
  returned by `GET /api/sources` and `GET /api/sources/:id`; databases created
  before the column existed are migrated automatically and read back as `[]`.
- **Dashboard:** the `OpenAPI` extra cell shows a compact `components N`
  indicator for a filtered statuspage source (or `–` when unset); component
  names and ids are not secrets but the indicator never renders the list.

```sh
# A statuspage that covers marketing, billing and the API: only ingest the API.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Acme Status","url":"https://status.acme.com/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Acme","tracked_versions":["v1"],
       "statuspage_components":["API","abc123"]}'
```

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
  - `data.changelogs.0.entries` → walk `data`, take the first element of the
    `changelogs` list, then collect its `entries`.
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

For every dotted path, a segment that is a non-negative integer indexes a list
(`0` is the first element) and `-1` addresses the last element, so a list nested
at a fixed position can be reached — for example `data.changelogs.0.entries`.
Invalid segments degrade gracefully and never raise: a numeric segment applied to
an object, a non-integer segment applied to a list, and an out-of-range index
(including `-1` against an empty list) all yield no value, so the parser returns
no entries exactly as it does for an unknown key.

Every source also accepts an optional `min_severity` (`critical`, `warning` or
`info`, default `info`). An entry is only turned into an alert when its severity
ranks at or above this floor; lower-severity entries are still parsed and stored.
An optional `notify_min_severity` adds a second, notification-only floor: when it
is set, an alert that clears `min_severity` but ranks below `notify_min_severity`
is still stored (visible through the API, the export and the dashboard) with
`notified = 0`, but is never fanned out by `Engine.dispatch` or claimed by a
digest flush. Omitting it (or sending `null`) means "inherit `min_severity`" and
is byte-for-byte equivalent to the pre-feature behaviour.

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

## Retention

API Deprecation Watchdog is a self-hosted, dependency-free service for platform
and integration teams and their on-call engineers. It keeps a full per-dependency
entry history and turns deprecations into prioritized alerts. The history is
useful, but an unbounded SQLite file is not: a deployment that runs for years
would otherwise retain every parsed changelog item forever, even long after the
alert it produced has been acted on. Retention bounds that growth without
weakening the product's guarantees.

- **Global default:** `RETENTION_DAYS` (integer, default `0`, floored at `0`)
  sets the deployment-wide window. `0` disables pruning, which is the legacy
  behaviour.
- **Per-source override:** the optional positive integer `retention_days` on a
  source overrides the global default for that dependency. Omitted or `null`
  leaves the source on the global default; `0`, negative, non-integer and
  boolean values are rejected with a `400` naming `retention_days`.
- **Prune pass:** `Store.prune_entries(retention_days, source_overrides, now)`
  deletes entries whose `created_at` is older than the effective window per
  source and deletes exactly the alerts whose `entry_id` was pruned, returning
  the counts. `Engine.prune()` supplies the per-source overrides and the current
  time; the scheduler calls it on a configurable maintenance cadence
  (`MAINTENANCE_INTERVAL_SECONDS`, default daily) and `POST /api/prune` triggers
  it on demand (token protected).
- **Never pruned:** `sources` and `digests` are never deleted by a prune, so the
  registry, delivered digests and the aggregate `stats` counters survive. Only
  the per-source entry history and the alerts derived from it are bounded.
- **Migration:** databases created before this feature gain the `retention_days`
  column automatically and read back as `null`, so existing deployments inherit
  the global default.

With `RETENTION_DAYS=0` and no per-source overrides a prune is a no-op that
returns zero counts, so existing deployments are unaffected until an operator
opts in.

```sh
# Keep the global history bounded at 90 days, but trim one chatty dependency to 7.
RETENTION_DAYS=90 python3 -m apiwatch

curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Chatty","url":"https://chatty.example/changelog.json",
       "kind":"json","retention_days":7}'

# Prune now (token protected when API_TOKEN is set).
curl -X POST http://localhost:8080/api/prune \
  -H "Authorization: Bearer $API_TOKEN"
# -> {"entries_pruned": 12, "alerts_pruned": 9}
```

## Entry-count cap

Retention bounds history by *age*, but a source that emits hundreds of entries a
day can still grow the store arbitrarily inside the window. The per-source
`max_entries` cap bounds history by *count* instead, so one chatty dependency
cannot crowd out a long-running deployment's SQLite file.

- **Field:** `max_entries` is an optional positive integer on a source. Omitted
  or `null` means "no cap"; `0`, negative integers, booleans, floats and strings
  are rejected with a `400` naming `max_entries`. The value is stored in a new
  `sources.max_entries` column and read back as `null` for databases created
  before the column existed (a lightweight migration adds it automatically).
- **When it runs:** `Engine.scan_source` calls
  `Store.prune_entries_for_source(source_id, max_entries)` after a successful
  scan, once it has persisted the scan's entries and recorded the truncation
  flag, so a chatty source is trimmed on every scan rather than only on the
  slower maintenance cadence. The number dropped is accumulated on
  `ScanResult.entries_capped` and reported by `/api/check` alongside the other
  per-scan counters.
- **What it keeps:** the newest `max_entries` entries by the same
  `created_at DESC, id DESC` order the history API uses, so
  `GET /api/sources/:id/entries` never shows more than the cap. The prune deletes
  only rows from `entries`; it never deletes `alerts`, `digests` or `sources`.
  An alert whose entry was capped away keeps its own stored `title` and `link`
  and still renders in `GET /api/alerts`, `/api/export` and the dashboard.
- **Composition with retention:** the two bounds are independent and the tighter
  one wins. Retention prunes by age in the maintenance pass and the cap prunes by
  count on each scan, so a source with `retention_days = D` and
  `max_entries = N` ends up holding at most `min(N, entries within D days)`.
  A source without a cap is never touched by the cap path, so the default
  behaviour is unchanged.
- **Observability:** `GET /api/stats` reports `entries_pruned`, the running total
  of entries removed by retention *and* by the cap since process start. It is `0`
  when no source has a cap and no retention window is set. The dashboard's
  `Retention` cell appends a compact `cap N` indicator next to the window (or
  `cap –` when the source has no cap) without adding a column.

```sh
# Keep at most the 200 newest entries for one chatty dependency.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Chatty","url":"https://chatty.example/changelog.json",
       "kind":"json","max_entries":200}'
```

## Alert cooldown

A single dependency can be noisy: a changelog may re-announce the same
deprecation in slightly different words, or emit several near-identical entries
within a short window. Content-hash de-duplication (`store.py`) removes exact
repeats, but near-duplicates still produce one alert each. The per-source
`alert_cooldown_seconds` flattens that burst without weakening the audit trail.

- **Field:** `alert_cooldown_seconds` is an optional non-negative integer on a
  source (default `None` / `0`). `null`, omitted and `0` all mean "no cooldown"
  and behave byte-for-byte like the pre-feature engine. A boolean, negative,
  non-integer or non-numeric value is rejected with a `400` naming
  `alert_cooldown_seconds`.
- **Semantics:** in `Engine.dispatch`, an alert is only fanned out when no alert
  from the same source was dispatched within the previous
  `alert_cooldown_seconds`. The last dispatch time is persisted per source
  (`sources.last_alert_at`) by the engine's clock, so the window survives a
  process restart rather than resetting on reboot.
- **Notification only:** a suppressed alert is *not* deleted or hidden. It is
  stored with `notified = 0`, appears in `GET /api/alerts` and `GET /api/entries`,
  participates in de-duplication as usual, and is still returned by
  `Store.list_undigested_alerts()`, so the next digest flush (`POST
  /api/digest/flush` or the digest timer) summarises it. This mirrors the
  `min_severity` philosophy: persistence is separate from alerting.
- **Accounting:** the number of alerts held back in a scan is reported as
  `alerts_suppressed` on the `POST /api/check` / `ScanResult.to_dict()` payload
  and aggregated across sources into `GET /api/stats`. The dashboard shows a
  `suppressed` card and a compact `cooldown Ns` indicator in each source's
  `Interval` cell; no secret value is ever rendered.
- **Migration:** databases created before this feature gain
  `alert_cooldown_seconds`, `last_alert_at` and `alerts_suppressed`
  automatically. Existing rows read back with a `null` cooldown, a `null` last
  dispatch time and a `0` suppressed count, so existing deployments are
  unchanged.

```sh
# A noisy vendor that repeats the same deprecation every few minutes: store
# every entry and alert it once, then coalesce repeats for an hour.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Noisy changelog","url":"https://noisy.example/changelog.json",
       "kind":"json","api_name":"Noisy","tracked_versions":["v2"],
       "alert_cooldown_seconds":3600}'
```

### Alert-cooldown jitter

A large registry often assigns the *same* `alert_cooldown_seconds` to many
sources, so their cooldown windows all expire on the same tick and their
coalesced alerts release in a burst. The optional
`alert_cooldown_jitter_seconds` spreads that release deterministically.

- **Field:** `alert_cooldown_jitter_seconds` is an optional non-negative integer
  on a source (default `None` / `0`). `null`, omitted and `0` all mean "no
  jitter" and behave byte-for-byte like the pre-feature engine (same dispatch
  decisions, same `alerts_suppressed` count). A boolean, negative, non-integer
  or non-numeric value is rejected with a `400` naming
  `alert_cooldown_jitter_seconds`.
- **Semantics:** the effective cooldown for a source becomes
  `alert_cooldown_seconds + (source.id % (alert_cooldown_jitter_seconds + 1))`
  when the jitter is set. The offset is derived from the source id — the same
  `id % (jitter + 1)` derivation as `scan_jitter_seconds` — and is never random,
  so the same `(id, jitter)` pair always yields the same offset and dispatch
  stays reproducible and testable. It composes with `alert_cooldown_seconds` and
  the other notification filters (the notification floor, the title-dedupe
  window and the severity escalation window): an alert must clear all of them,
  applied to the jittered cooldown, to be delivered.
- **No cooldown:** a source with `alert_cooldown_seconds` unset or `0` is
  unaffected by any `alert_cooldown_jitter_seconds` value, because the jitter is
  added to a zero window and the source has no cooldown to begin with.
- **Notification only:** like the base cooldown, a held-back alert is *not*
  deleted or hidden. It is stored with `notified = 0`, appears in
  `GET /api/alerts`, `GET /api/entries` and `GET /api/export`, and stays eligible
  for a later digest. `last_alert_at` is still persisted per source, so a restart
  does not reset the (jittered) window.
- **Accounting:** the held-back alerts are added to the same `alerts_suppressed`
  counter the cooldown uses and aggregated by `GET /api/stats`. The dashboard
  appends a compact `+0-Ns` window after the `cooldown Ns` indicator when the
  jitter is set (for example `cooldown 300s +0-60s`); the base `cooldown Ns` /
  `dedupe Ns` / `escalate Ns` output is unchanged when the jitter is unset, and
  no secret value is ever rendered.
- **Migration:** databases created before this feature gain
  `alert_cooldown_jitter_seconds` automatically, with old rows read back as
  `null` (no jitter), so existing deployments are unchanged.

```sh
# 50 sources share a one-hour cooldown against the same upstream: spread their
# release over an extra deterministic 0-60s window per source id.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Shared upstream","url":"https://vendor.example/changelog.json",
       "kind":"json","api_name":"Vendor","tracked_versions":["v2"],
       "alert_cooldown_seconds":3600,"alert_cooldown_jitter_seconds":60}'
```

## Alert dedupe window

Content-hash de-duplication removes byte-for-byte repeats, and
`alert_cooldown_seconds` flattens a source's whole alert stream. Neither catches
a vendor that re-posts the *same* deprecation announcement with a tweaked body
or a new `published` date: the body differs, so the content hash differs, and if
the source also emits other distinct alerts the cooldown alone does not stop the
repeat. The per-source `dedupe_window_seconds` closes that gap by keying the
window on the alert's normalized title instead of the whole source.

- **Field:** `dedupe_window_seconds` is an optional non-negative integer on a
  source (default `None` / `0`). `null`, omitted and `0` all mean "disabled" and
  behave byte-for-byte like the pre-feature engine (same dispatch decisions, same
  `alerts_suppressed` count, no `last_title_alerts` writes). A boolean, negative,
  non-integer or non-numeric value is rejected with a `400` naming
  `dedupe_window_seconds`.
- **Normalization:** titles are compared after trimming, collapsing internal
  whitespace runs to one space and case-folding, so `"v1 deprecated"`,
  `" V1  Deprecated "` and `"v1\nDeprecated"` share one window.
- **Difference from cooldown:** `alert_cooldown_seconds` is time-since-last-
  dispatch *of any alert* from the source and silences the whole source for the
  window; `dedupe_window_seconds` is time-since-last-dispatch *of the same
  normalized title* and therefore lets a genuinely new title through even inside
  another title's window. The two compose: an alert must clear the notification
  floor, the cooldown and the dedupe window to be delivered.
- **Notification only:** a held-back alert is stored with `notified = 0`, appears
  in `GET /api/alerts`, `GET /api/entries` and `GET /api/export`, and remains
  eligible for a later digest flush once the window elapses. Entries are always
  stored.
- **Persistence:** each delivered title's dispatch time is written to
  `sources.last_title_alerts` (a JSON map of normalized title -> ISO-8601 time)
  so the window survives a process restart; a source that never dispatched reads
  back as `{}` and nothing is held back.
- **Accounting:** the held-back alerts are added to the same `alerts_suppressed`
  counter the cooldown uses, aggregated by `GET /api/stats`. The dashboard shows
  a compact `dedupe Ns` indicator next to `cooldown Ns` in the `Interval`
  column, or `–` when the field is unset; no secret values are rendered.
- **Migration:** databases created before this feature gain
  `dedupe_window_seconds` and `last_title_alerts` automatically. Existing rows
  read back with a `null` window and an empty title map, so existing deployments
  are unchanged.

```sh
# A statuspage that re-posts the same incident headline every few minutes with a
# new body: store every entry, alert once, then coalesce repeats for 30 minutes.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Payments status","url":"https://status.payments.example/api/v2/incidents.json",
       "kind":"statuspage","api_name":"Payments","tracked_versions":["v1"],
       "dedupe_window_seconds":1800}'
```

## Severity escalation window

Content-hash de-duplication removes exact repeats, `alert_cooldown_seconds`
flattens a source's whole alert stream and `dedupe_window_seconds` catches the
same title. None of them stops a vendor that re-announces the *same severity* of
change every few days with a drifting title and body: each announcement is a new
content hash, a new title and — if the source also emits other alerts — a fresh
cooldown. The per-source `escalation_window_seconds` closes that gap by keying a
window on the alert's severity.

- **Field:** `escalation_window_seconds` is an optional non-negative integer on a
  source (default `None` / `0`). `null`, omitted and `0` all mean "disabled" and
  behave byte-for-byte like the pre-feature engine (same dispatch decisions, same
  `alerts_suppressed` count, no `last_severity_alerts` writes). A boolean,
  negative, non-integer or non-numeric value is rejected with a `400` naming
  `escalation_window_seconds`.
- **Semantics:** in `Engine.dispatch`, an alert at or above the source's
  effective notification floor (`notify_min_severity or min_severity`) is only
  fanned out when no alert of the *same severity* from the same source was
  dispatched within the previous `escalation_window_seconds`. A higher-severity
  alert always escalates through, so after a `warning` is delivered a `critical`
  is still dispatched, but a repeat `critical` inside the window is held back.
- **Difference from cooldown/dedupe:** the cooldown silences the whole source for
  its window and the dedupe window silences one normalized title; the escalation
  window silences one severity, letting a genuinely new lower-notify-floor
  severity through while coalescing repeats of the same one. All three compose:
  an alert must clear the notification floor, the cooldown, the dedupe window and
  the escalation window to be delivered.
- **Notification only:** a held-back alert is stored with `notified = 0`, appears
  in `GET /api/alerts`, `GET /api/entries` and `GET /api/export`, and remains
  eligible for a later digest flush once the window elapses. Entries are always
  stored.
- **Persistence:** each delivered severity's dispatch time is written to
  `sources.last_severity_alerts` (a JSON map of severity -> ISO-8601 time,
  merged last-write-wins) so the window survives a process restart; a source that
  never dispatched reads back as `{}` and nothing is held back.
- **Accounting:** the held-back alerts are added to the same `alerts_suppressed`
  counter the cooldown and dedupe window use, aggregated by `GET /api/stats`.
  The dashboard shows a compact `escalate Ns` indicator next to `cooldown Ns` and
  `dedupe Ns` in the `Interval` column, or `–` when the field is unset; no secret
  values are rendered.
- **Migration:** databases created before this feature gain
  `escalation_window_seconds` and `last_severity_alerts` automatically. Existing
  rows read back with a `null` window and an empty severity map, so existing
  deployments are unchanged.

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

## Notification floor

`min_severity` controls whether an alert is *created* at all. Sometimes an
operator wants the opposite split: keep the `info` rows for the audit trail, but
never notify on anything below `critical`. `notify_min_severity` is that second,
notification-only floor.

- **Field:** `notify_min_severity` is an optional `critical`, `warning` or `info`
  on a source (case-insensitive, trimmed). Omitted or `null` means "inherit
  `min_severity`" and is stored as `NULL`; any other value is rejected with a
  `400` naming `notify_min_severity`. It may be set independently of
  `min_severity` — for example `min_severity: "info"` with
  `notify_min_severity: "critical"` records `info` and `warning` alerts without
  ever notifying on them.
- **Effective floor:** the engine computes
  `source.notify_min_severity or source.min_severity` per alert and reuses the
  shared `meets_floor` comparison from `relevance.py`, so the notification floor
  and the storage floor cannot drift apart.
- **Dispatch:** in `Engine.dispatch`, an alert below the effective floor is not
  fanned out and is *not* marked notified. It counts towards `alerts_suppressed`
  and composes with `alert_cooldown_seconds`: an alert must clear both filters to
  be delivered.
- **Digests:** `flush_digest` passes only alerts that clear their source's floor
  to the pure `build_digest` builder, so `DIGEST_MODE=digest`/`both` honours the
  same rule. A below-floor alert is left pending (`notified = 0`,
  `digest_id IS NULL`) rather than claimed, so lowering the floor later can still
  surface it; a `digest` scan counts the held-back alerts once, and a flush never
  re-counts them.
- **Notification only:** a held-back alert is *not* deleted or hidden. It appears
  in `GET /api/alerts`, `GET /api/entries` and `GET /api/export`, participates in
  de-duplication as usual, and carries `notified = 0`. The dashboard shows a
  compact `notify: <severity>` indicator next to the `Min severity` badge, or an
  en-dash (`–`) when the source inherits `min_severity`; no secret values are
  rendered.
- **Migration:** databases created before this feature gain the
  `notify_min_severity` column automatically. Existing rows read back as `NULL`,
  i.e. they inherit `min_severity`, so omitting the field reproduces the
  pre-feature behaviour exactly (same dispatch set, same `notified` flags, same
  `alerts_suppressed` count, same digest contents).

```sh
# Record info and warning alerts for history, but only ever notify on critical.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Vendor SDK","url":"https://vendor.example/changelog.json",
       "kind":"json","api_name":"Vendor","tracked_versions":["v2"],
       "min_severity":"info","notify_min_severity":"critical"}'
```

## Alert fan-out cap

The notification floor, the cooldown, the title-dedupe window and the severity
escalation window all bound *when* a source may notify. None of them bounds
*how many* alerts a single scan may fan out: a vendor that dumps a batch of
genuinely distinct entries (ten breaking changes, a statuspage that flaps, an
OpenAPI diff that surfaces dozens of structural changes at once) still fires one
message per alert in one tick. The per-source `max_alerts_per_scan` caps that
fan-out by count.

- **Field:** `max_alerts_per_scan` is an optional positive integer on a source
  (default `None`, i.e. no cap). `null` and omitted both mean "no cap" and
  behave byte-for-byte like the pre-feature engine (same dispatch calls, same
  `alerts_suppressed` count, same stored rows). A boolean, non-integer or
  non-positive value is rejected with a `400` naming `max_alerts_per_scan`.
- **Semantics:** in `Engine.scan_source`, when the source has a cap the new
  alerts are ordered by severity rank (`critical` > `warning` > `info`) with a
  *stable* sort, so equal-severity alerts keep their creation order, and only
  the first `N` are handed to `Engine.dispatch`. The rest are held back from
  this scan's fan-out. A source with fewer than `N` new alerts, or no cap, is
  untouched.
- **Digest composition:** the cap is a dispatch-time filter. With
  `DIGEST_MODE=off` and `both` (`both` dispatches immediately with
  `mark=False`) it limits the immediate fan-out; with `DIGEST_MODE=digest`
  nothing is dispatched per scan, so the cap is deferred and does not interact
  with the notification-floor accounting (an alert is never counted twice).
- **Notification only:** a held-back alert is *not* deleted or hidden. It is
  stored with `notified = 0`, appears in `GET /api/alerts`, `GET /api/entries`
  and `GET /api/export`, participates in de-duplication as usual, and is still
  returned by `Store.list_undigested_alerts()`, so the next digest flush
  summarises it. Entries are always stored. This mirrors the `min_severity` /
  notification-floor philosophy: persistence is separate from alerting.
- **Accounting:** the number of alerts held back by the cap in a scan is added
  to `alerts_suppressed` on the `ScanResult` / `POST /api/check` payload and
  aggregated across sources into `GET /api/stats`. The dashboard shows a compact
  `fanout N` indicator in each source's `Interval` cell, or `fanout –` when the
  source is uncapped; no secret value is ever rendered.
- **Migration:** databases created before this feature gain
  `max_alerts_per_scan` automatically. Existing rows read back as `NULL`, so a
  deployment that never sets the field is unchanged.

```sh
# A vendor that sometimes dumps dozens of breaking changes at once: store every
# alert but fan out at most 3 per scan, highest severity first.
curl -X POST http://localhost:8080/api/sources \
  -H 'Content-Type: application/json' \
  -d '{"name":"Batch vendor","url":"https://vendor.example/changelog.json",
       "kind":"json","api_name":"Vendor","tracked_versions":["v2"],
       "max_alerts_per_scan":3}'
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

*Shipped:* **per-source OpenAPI baseline and ignore paths** (see *Current
capabilities* and *OpenAPI baseline and ignore paths*) — an optional
`openapi_baseline` object pins the spec a live document is diffed against, so a
first scan reports only the changes since that spec, and `openapi_ignore_paths`
mutes known-irrelevant structural changes by exact path or `/`-boundary prefix
without lowering `min_severity`. Both default to off, so a source that sets
neither behaves exactly as before, and the dashboard shows a compact
`baseline` / `ignore N` indicator rather than the pinned payload.

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
`WEBHOOK_URL`, plus a per-source `notify_webhook_url` override that routes both
that source's alerts *and* its digests to a dedicated endpoint; omitting the
field falls back to the global `WEBHOOK_URL`. This advances the *Integrations*
item below: alert routing no longer requires a hard-coded integration for each
tracker or paging system.

**Remaining webhook work (not yet shipped):** the per-source override selects the
right endpoint, but delivery hardening is still open. Two follow-ups are
explicit next steps rather than shipped behaviour: **per-source retry/backoff**
(a failed POST is currently logged and left pending for the next scan or digest
flush, with no bounded retry window or exponential backoff), and an optional
**signature header** so a receiving endpoint can verify that a payload was sent
by the watchdog and has not been tampered with.

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

*Shipped:* **bounded history (retention)** (see *Current capabilities* and
*Retention*) — a global `RETENTION_DAYS` default plus an optional per-source
`retention_days` define how long entries (and the alerts derived from them)
survive; a scheduler maintenance pass and the token-protected `POST /api/prune`
endpoint prune on the chosen cadence, while `sources` and `digests` are never
deleted. This is the next increment on top of the per-source history API and
keeps a long-running deployment's SQLite file bounded.

*Shipped:* **per-source entry-count cap** (see *Current capabilities* and
*Entry-count cap*) — an optional positive `max_entries` trims each source to its
N newest entries after every successful scan, complementing the age-based
`retention_days` window so the tighter of the two bounds wins. The cap never
deletes alerts or digests, so alerts whose entries were dropped still render with
their stored title and link, and the total removed (retention plus cap) is
surfaced as `entries_pruned` in `GET /api/stats` while the per-scan drop count is
reported as `entries_capped`. The dashboard shows a compact `cap N` indicator
next to the retention label, and omitting the field keeps the pre-feature
behaviour byte-for-byte.

*Shipped:* **per-source alert cooldown** (see *Current capabilities* and
*Alert cooldown*) — an optional `alert_cooldown_seconds` on each source that
coalesces repeat notifications from a noisy dependency for a configurable
window, persisting the last dispatch time so the window survives a restart and
counting the held-back alerts as `alerts_suppressed`. Suppressed alerts stay
stored with `notified = 0` and are summarised by the next digest, so persistence
and alerting remain separate. This directly advances the *low noise / smarter
relevance* goal: a re-announced deprecation no longer floods Slack, e-mail or
the webhook channel with duplicates.

*Shipped:* **per-source alert-cooldown jitter** (see *Current capabilities* and
*Alert cooldown*) — an optional `alert_cooldown_jitter_seconds` that adds a
deterministic, id-derived offset
(`id % (jitter + 1)`) to a source's effective `alert_cooldown_seconds`, so a
large registry in which many sources share the same cooldown no longer releases
their coalesced alerts on the same tick. It mirrors `scan_jitter_seconds` but
applies to notification dispatch; it composes with the other notification
filters, is notification-only (held-back alerts stay stored with
`notified = 0` and count as `alerts_suppressed`), leaves a source with no
cooldown untouched, adds a compact `+0-Ns` dashboard indicator and keeps the
pre-feature behaviour byte-for-byte when the field is unset, `null` or `0`.

*Shipped:* **per-source notification floor** (see *Current capabilities* and
*Notification floor*) — an optional `notify_min_severity` that defaults to
inheriting `min_severity` and separates persistence from alerting a second time:
an alert may be stored as history without ever being fanned out by
`Engine.dispatch` or claimed by a digest flush. It reuses the shared
`meets_floor` comparison, composes with the cooldown (an alert must clear both),
counts held-back alerts as `alerts_suppressed`, adds a `notify: <severity>`
dashboard indicator and migrates old databases with the column left `NULL`. This
directly advances the *Smarter relevance* item below: teams can now keep a full
`info` audit trail while paging only on `critical`.

*Shipped:* **array-index selectors and entry pagination** (see *Current
capabilities* and *Selector syntax*) — numeric segments (`0`, `1`, … and `-1`
for the last element) in the dotted `json`/`github`/`gitlab`/`openapi` selector,
implemented in one shared walker and degrading to no entries for invalid input,
plus an optional `?offset=` (non-negative, default `0`) on `GET /api/entries`
and `GET /api/sources/:id/entries` that composes with `?limit=`. The default,
offset-free pages are byte-for-byte unchanged.

*Shipped:* **per-source severity escalation window** (see *Current capabilities*
and *Severity escalation window*) — an optional `escalation_window_seconds` that
holds back a repeat notification of the same severity from one source for a
configurable window while letting a higher severity escalate through. It
composes with the notification floor, the cooldown and the title-dedupe window,
persists the per-severity dispatch times so the window survives a restart,
counts held-back alerts as `alerts_suppressed`, adds an `escalate Ns` dashboard
indicator and leaves the pre-feature behaviour byte-for-byte unchanged when the
field is unset or `0`. This closes the last gap in the *low-noise* story: a
vendor that re-announces the same-severity change with a drifting title no
longer pages on every re-announcement.

*Shipped:* **per-source per-scan alert fan-out cap** (see *Current capabilities*
and *Alert fan-out cap*) — an optional positive `max_alerts_per_scan` that
bounds how many alerts one scan may fan out, ordering the new alerts by severity
(`critical` first, stable on creation order) so the most dangerous changes
always get through. The held-back alerts stay stored with `notified = 0` and
remain digest-eligible, count as `alerts_suppressed` in `GET /api/stats`, and
the dashboard shows a compact `fanout N` indicator. It composes with the
notification floor, cooldown, dedupe and escalation filters and, in
`DIGEST_MODE=digest`, leaves the floor accounting untouched (the cap is a
dispatch-time filter). Omitting the field keeps the pre-feature behaviour
byte-for-byte. This completes the *low-noise* story: the per-source notification
throttles now bound both *when* and *how many* alerts a source can emit per scan.

*Shipped:* **per-source status-page component filter** (see *Current capabilities*
and *Status-page component filter*) — an optional `statuspage_components` list
that narrows a `statuspage` source to the components (matched by name
case-insensitively or by id exactly) a team actually depends on. An item that
names no component is kept only when the list is empty, the empty-filter path is
byte-for-byte unchanged, and the dashboard shows a compact `components N`
indicator. This directly advances the *Smarter relevance* item below: a page that
covers many unrelated components no longer erodes trust in the alert stream.

**Next candidate (not yet shipped):** extend the same component-scoped idea to
the status/impact axis — an optional per-source filter on an incident's `status`
(for example ignore `resolved`) or `impact` (for example only
`critical`/`major`) — and to statuspage component *groups*, so operators can
scope even more precisely without muting an entire source.

**Follow-ups this unlocks (not yet shipped):** the pagination primitive is in
place but the dashboard has no "load more" affordance yet, and `GET /api/export`
still exposes only `?limit=` and returns the newest page with no `offset`. Those
remain explicit next steps rather than shipped behaviour.

1. **More source adapters:** additional structured feeds beyond the shipped
   GitHub and GitLab releases and OpenAPI adapters. *Partially shipped:*
   the `gitlab` adapter now covers GitLab-hosted releases; further forges and
   trackers remain.
2. **Smarter relevance:** semantic matching of version ranges (semver / date
   versions), per-team severity rules and richer keyword tuning. *Partially
   shipped:* per-source `min_severity`, `notify_min_severity` and
   `notify_channels` already let a team tune the volume and destination of alerts
   per dependency, and the shipped per-source `extra_keywords` /
   `suppress_keywords` overrides let a team extend or mute the detection
   vocabulary without forking the service.
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
A third, independent maintenance tick calls `engine.prune()` on the
`MAINTENANCE_INTERVAL_SECONDS` cadence to apply the retention windows, sharing
the same lock so it never races a scan or a digest flush. The token-protected
`POST /api/prune` endpoint calls the same `engine.prune()` primitive.

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
