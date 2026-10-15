import unittest
from datetime import datetime, timezone

from apiwatch.digest import build_digest
from apiwatch.models import Alert

NOW = datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def alert(source_id, title, severity="info", created_at="2024-01-01T00:00:00+00:00", alert_id=None):
    return Alert(
        source_id=source_id,
        entry_id=alert_id or 0,
        severity=severity,
        reason="deprecated",
        title=title,
        link="https://example.com/%s" % title,
        id=alert_id,
        created_at=created_at,
    )


class BuildDigestTests(unittest.TestCase):
    def test_empty_input(self):
        digest = build_digest([], {}, now=NOW)
        self.assertEqual(digest.alert_count, 0)
        self.assertEqual(digest.omitted, 0)
        self.assertEqual(digest.severity, "info")
        self.assertIn("Alerts: 0", digest.body)
        self.assertEqual(digest.alert_ids, [])

    def test_groups_by_source(self):
        alerts = [
            alert(1, "one", alert_id=1),
            alert(2, "two", alert_id=2),
            alert(1, "three", alert_id=3),
        ]
        digest = build_digest(alerts, {1: "Alpha", 2: "Beta"}, now=NOW)
        self.assertIn("== Alpha ==", digest.body)
        self.assertIn("== Beta ==", digest.body)

    def test_groups_ordered_by_severity_then_name(self):
        alerts = [
            alert(1, "info-alert", severity="info", alert_id=1),
            alert(2, "critical-alert", severity="critical", alert_id=2),
            alert(3, "warning-alert", severity="warning", alert_id=3),
        ]
        names = {1: "Alpha", 2: "Beta", 3: "Gamma"}
        digest = build_digest(alerts, names, now=NOW)
        body = digest.body
        self.assertLess(body.index("== Beta =="), body.index("== Gamma =="))
        self.assertLess(body.index("== Gamma =="), body.index("== Alpha =="))
        self.assertEqual(digest.severity, "critical")

    def test_alerts_within_group_newest_first(self):
        alerts = [
            alert(1, "older", created_at="2024-01-01T00:00:00+00:00", alert_id=1),
            alert(1, "newer", created_at="2024-02-01T00:00:00+00:00", alert_id=2),
        ]
        digest = build_digest(alerts, {1: "Alpha"}, now=NOW)
        self.assertLess(digest.body.index("newer"), digest.body.index("older"))

    def test_max_alerts_truncation_and_omitted(self):
        alerts = [alert(1, "a%d" % i, alert_id=i) for i in range(1, 6)]
        digest = build_digest(alerts, {1: "Alpha"}, max_alerts=2, now=NOW)
        self.assertEqual(digest.alert_count, 2)
        self.assertEqual(digest.omitted, 3)
        self.assertIn("Omitted: 3", digest.body)
        self.assertEqual(len(digest.alert_ids), 5)

    def test_deterministic_for_fixed_now(self):
        alerts = [alert(1, "a", alert_id=1), alert(2, "b", severity="warning", alert_id=2)]
        names = {1: "Alpha", 2: "Beta"}
        first = build_digest(alerts, names, now=NOW)
        second = build_digest(alerts, names, now=NOW)
        self.assertEqual(first.body, second.body)
        self.assertEqual(first.subject, second.subject)
        self.assertIn("Generated: 2024-03-01T12:00:00+00:00", first.body)
        self.assertEqual(first.subject, "API Deprecation Watchdog digest: 2 alerts")


if __name__ == "__main__":
    unittest.main()
