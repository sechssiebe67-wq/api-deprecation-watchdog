"""Server-rendered HTML dashboard.

Deliberately dependency-free: a single escaped HTML document with inline CSS so
the service works even without a template engine or asset pipeline.
"""
import html
from datetime import datetime
from typing import List, Optional

from .models import Alert, Digest, Source, parse_iso8601, utcnow

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       background: #0f172a; color: #e2e8f0; }
header { padding: 1.5rem; background: #111c33; border-bottom: 1px solid #1f2a44; }
h1 { margin: 0; font-size: 1.4rem; }
h2 { margin: 1.5rem 0 .5rem; font-size: 1.1rem; }
main { padding: 0 1.5rem 3rem; max-width: 1100px; margin: 0 auto; }
.cards { display: flex; flex-wrap: wrap; gap: .75rem; margin-top: 1rem; }
.card { background: #16233d; border: 1px solid #24324f; border-radius: .6rem;
        padding: .75rem 1rem; min-width: 9rem; }
.card .num { font-size: 1.6rem; font-weight: 700; }
.card .label { color: #94a3b8; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
table { width: 100%; border-collapse: collapse; margin-top: .5rem; font-size: .9rem; }
th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid #24324f; vertical-align: top; }
th { color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: .72rem; letter-spacing: .05em; }
a { color: #7dd3fc; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 999px; font-size: .72rem; font-weight: 700; }
.critical { background: #7f1d1d; color: #fecaca; }
.warning { background: #78350f; color: #fed7aa; }
.info { background: #1e3a8a; color: #bfdbfe; }
.muted { color: #94a3b8; }
code { background: #0b1220; padding: .1rem .3rem; border-radius: .3rem; }
@media (max-width: 600px) { .col-hide { display: none; } main { padding: 0 .75rem 2rem; } }
"""


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _badge(severity: str) -> str:
    severity = severity if severity in ("critical", "warning", "info") else "info"
    return '<span class="badge %s">%s</span>' % (severity, _esc(severity.upper()))


def _truncate(value: str, limit: int = 48) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "\u2026"


def render_dashboard(
    stats: dict,
    sources: List[Source],
    alerts: List[Alert],
    digests: List[Digest] = None,
    default_interval: int = None,
    now: Optional[datetime] = None,
) -> str:
    digests = digests or []
    now = now or utcnow()
    cards = [
        ("sources", stats.get("sources", 0)),
        ("enabled", stats.get("sources_enabled", 0)),
        ("entries", stats.get("entries", 0)),
        ("alerts", stats.get("alerts", 0)),
        ("critical", stats.get("alerts_critical", 0)),
        ("warning", stats.get("alerts_warning", 0)),
        ("pending digest", stats.get("alerts_pending_digest", 0)),
    ]
    card_html = "".join(
        '<div class="card"><div class="num">%s</div><div class="label">%s</div></div>'
        % (_esc(value), _esc(label))
        for label, value in cards
    )

    def _interval_label(source: Source) -> str:
        if source.scan_interval_seconds is not None:
            return "%ss" % source.scan_interval_seconds
        if default_interval is not None:
            return "%ss (global)" % default_interval
        return "global"

    def _auth_indicator(source: Source) -> str:
        """A compact lock/header-count badge that never reveals secret values."""
        parts = []
        if source.auth:
            parts.append("\U0001F512")
        if source.headers:
            parts.append("%d hdr" % len(source.headers))
        return " ".join(parts)

    def _health_cell(source: Source) -> str:
        """Render the breaker health without ever exposing headers/credentials."""
        if source.breaker_until:
            try:
                until = parse_iso8601(source.breaker_until)
            except ValueError:
                until = None
            if until is not None and until > now:
                label = "tripped until %s" % until.strftime("%Y-%m-%d %H:%M UTC")
                return (
                    '<span class="critical">%s</span> '
                    '<form method="post" action="/api/sources/%s/reset" '
                    'style="display:inline"><button type="submit">Reset</button>'
                    "</form>" % (_esc(label), _esc(source.id))
                )
        if source.failure_count:
            noun = "failure" if source.failure_count == 1 else "failures"
            return '<span class="warning">%s %s</span>' % (
                _esc(source.failure_count),
                noun,
            )
        return '<span class="info">ok</span>'

    if sources:
        source_rows = "".join(
            "<tr><td>%s</td><td><code>%s</code></td><td class=\"col-hide\">%s</td>"
            "<td class=\"col-hide\">%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td class=\"col-hide muted\">%s</td><td class=\"col-hide\">%s</td>"
            "<td>%s</td><td>%s</td>"
            "<td><a href=\"/api/sources/%s/entries\">History</a></td></tr>"
            % (
                _esc(source.name),
                _esc(source.kind),
                _esc(source.api_name),
                _esc(", ".join(source.tracked_versions)),
                _esc(_interval_label(source)),
                _badge(source.min_severity),
                _esc(", ".join(source.notify_channels)),
                _esc(_truncate(source.notify_webhook_url) if source.notify_webhook_url else "\u2013"),
                _esc(_auth_indicator(source)),
                _health_cell(source),
                _esc("enabled" if source.enabled else "disabled"),
                _esc(source.id),
            )
            for source in sources
        )
    else:
        source_rows = '<tr><td colspan="12" class="muted">No sources registered yet.</td></tr>'

    if alerts:
        alert_rows = ""
        for alert in alerts:
            link = (
                '<a href="%s">%s</a>' % (_esc(alert.link), _esc(alert.title or alert.link))
                if alert.link
                else _esc(alert.title)
            )
            alert_rows += (
                "<tr><td>%s</td><td>%s</td><td class=\"col-hide\">%s</td>"
                "<td class=\"col-hide muted\">%s</td></tr>"
                % (
                    _badge(alert.severity),
                    link,
                    _esc(alert.reason),
                    _esc(alert.created_at),
                )
            )
    else:
        alert_rows = '<tr><td colspan="4" class="muted">No alerts detected yet.</td></tr>'

    if digests:
        digest_rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _esc(digest.created_at),
                _esc(digest.alert_count),
                _badge(digest.severity),
                _esc("delivered" if digest.delivered else "pending"),
            )
            for digest in digests
        )
    else:
        digest_rows = '<tr><td colspan="4" class="muted">No digests yet.</td></tr>'

    last_check = stats.get("last_check") or {}
    last_line = (
        "Last check: %s (%s sources, %s alerts, %s errors)"
        % (
            _esc(last_check.get("finished_at") or last_check.get("started_at") or "never"),
            _esc(last_check.get("sources_checked", 0)),
            _esc(last_check.get("alerts_created", 0)),
            _esc(last_check.get("errors", 0)),
        )
    )

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>API Deprecation Watchdog</title>
<style>%s</style>
</head>
<body>
<header>
  <h1>API Deprecation Watchdog</h1>
  <div class="muted">%s</div>
</header>
<main>
  <div class="cards">%s</div>
  <h2>Recent alerts</h2>
  <table>
    <thead><tr><th>Severity</th><th>Title</th><th class="col-hide">Reason</th><th class="col-hide">Detected</th></tr></thead>
    <tbody>%s</tbody>
  </table>
  <h2>Recent digests</h2>
  <table>
    <thead><tr><th>Created</th><th>Alerts</th><th>Severity</th><th>Delivered</th></tr></thead>
    <tbody>%s</tbody>
  </table>
  <h2>Monitored sources</h2>
  <table>
    <thead><tr><th>Name</th><th>Kind</th><th class="col-hide">API</th><th class="col-hide">Versions</th><th>Interval</th><th>Min severity</th><th>Channels</th><th class="col-hide">Webhook</th><th class="col-hide">Auth</th><th>Health</th><th>Status</th><th>History</th></tr></thead>
    <tbody>%s</tbody>
  </table>
  <p class="muted">JSON API: <code>/api/sources</code>, <code>/api/sources/:id/entries</code>, <code>/api/sources/:id/reset</code> (POST), <code>/api/alerts</code>, <code>/api/entries</code>, <code>/api/digests</code>, <code>/api/stats</code>, <code>/api/check</code> (POST), <code>/api/digest/flush</code> (POST).</p>
</main>
</body>
</html>
""" % (
        _CSS,
        last_line,
        card_html,
        alert_rows,
        digest_rows,
        source_rows,
    )
