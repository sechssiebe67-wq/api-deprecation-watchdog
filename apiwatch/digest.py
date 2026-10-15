"""Digest builder.

Turns a batch of pending alerts into a single grouped summary. This module is
pure: it performs no I/O and is deterministic for a fixed ``now`` so it can be
unit tested offline.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .models import Alert, Digest
from .notify import format_alert

_SEVERITY_ORDER = ("critical", "warning", "info")
_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def _rank(severity: str) -> int:
    return _SEVERITY_RANK.get(severity, 0)


def build_digest(
    alerts: Sequence[Alert],
    source_names: Dict[int, str],
    max_alerts: int = 50,
    now: Optional[datetime] = None,
) -> Digest:
    """Group ``alerts`` by source and render a single digest message.

    Groups are ordered by the highest severity they contain (critical >
    warning > info) and then by source name; alerts inside a group are ordered
    newest-first. At most ``max_alerts`` alerts are rendered and the number of
    omitted alerts is recorded.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    generated = now.isoformat(timespec="seconds")

    max_alerts = max(0, int(max_alerts))
    total = len(alerts)

    groups: Dict[int, List[Alert]] = {}
    for alert in alerts:
        groups.setdefault(alert.source_id, []).append(alert)

    def group_sort_key(item):
        source_id, items = item
        highest = max((_rank(alert.severity) for alert in items), default=0)
        return (-highest, source_names.get(source_id, "") or "", source_id)

    ordered_groups = sorted(groups.items(), key=group_sort_key)

    selected: Dict[int, List[Alert]] = {}
    included = 0
    for source_id, items in ordered_groups:
        items = sorted(
            items,
            key=lambda alert: (alert.created_at or "", alert.id or 0),
            reverse=True,
        )
        for alert in items:
            if included >= max_alerts:
                break
            selected.setdefault(source_id, []).append(alert)
            included += 1

    omitted = total - included
    overall = "info"
    if alerts:
        best = max(_rank(alert.severity) for alert in alerts)
        overall = next(
            (name for name in _SEVERITY_ORDER if _rank(name) == best), "info"
        )

    lines = [
        "API Deprecation Watchdog digest",
        "Generated: %s" % generated,
        "Alerts: %d" % total,
    ]
    if omitted:
        lines.append("Omitted: %d (showing first %d)" % (omitted, max_alerts))
    lines.append("")

    for source_id, items in ordered_groups:
        group_alerts = selected.get(source_id)
        if not group_alerts:
            continue
        name = source_names.get(source_id, "") or ("source %s" % source_id)
        lines.append("== %s ==" % name)
        for alert in group_alerts:
            lines.append(format_alert(alert, name))
            lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    subject = "API Deprecation Watchdog digest: %d alert%s" % (
        total,
        "" if total == 1 else "s",
    )

    return Digest(
        alert_count=included,
        severity=overall,
        body=body,
        subject=subject,
        omitted=omitted,
        created_at=generated,
        alert_ids=[alert.id for alert in alerts if alert.id is not None],
    )
