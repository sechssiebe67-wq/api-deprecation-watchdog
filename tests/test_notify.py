import json
import unittest
from unittest.mock import patch

from apiwatch.models import Alert, Digest
from apiwatch.notify import NotificationError, WebhookNotifier


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Recorder:
    """Stands in for ``urllib.request.urlopen`` and records requests."""

    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status)


def _alert(alert_id=1, source_id=1):
    return Alert(
        source_id=source_id,
        entry_id=alert_id,
        severity="critical",
        reason="v1 will be removed",
        matched_terms=["deprecated"],
        version_hits=["v1"],
        title="v1 deprecated",
        link="https://example.test/notes",
        id=alert_id,
    )


class WebhookNotifierTests(unittest.TestCase):
    def test_send_posts_expected_payload(self):
        recorder = Recorder(200)
        notifier = WebhookNotifier("https://example.test/hook")
        with patch("apiwatch.notify.urlopen", recorder):
            notifier.send([_alert()], {1: "Example"})
        self.assertEqual(len(recorder.requests), 1)
        request = recorder.requests[0]
        self.assertEqual(request.full_url, "https://example.test/hook")
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["source_names"], {"1": "Example"})
        self.assertEqual(payload["alerts"], [_alert().to_dict()])

    def test_send_digest_posts_expected_payload(self):
        recorder = Recorder(200)
        notifier = WebhookNotifier("https://example.test/hook")
        digest = Digest(
            alert_count=1,
            severity="critical",
            body="body",
            subject="subject",
            id=7,
            created_at="2024-01-01T00:00:00+00:00",
        )
        with patch("apiwatch.notify.urlopen", recorder):
            notifier.send_digest(digest, {1: "Example"})
        self.assertEqual(len(recorder.requests), 1)
        payload = json.loads(recorder.requests[0].data.decode("utf-8"))
        self.assertEqual(payload, {"digest": digest.to_dict()})

    def test_url_override_is_used(self):
        recorder = Recorder(200)
        notifier = WebhookNotifier("https://example.test/hook")
        with patch("apiwatch.notify.urlopen", recorder):
            notifier.send([_alert()], {}, url="https://example.test/critical")
        self.assertEqual(
            recorder.requests[0].full_url, "https://example.test/critical"
        )

    def test_empty_alerts_sends_nothing(self):
        recorder = Recorder(200)
        notifier = WebhookNotifier("https://example.test/hook")
        with patch("apiwatch.notify.urlopen", recorder):
            notifier.send([], {})
        self.assertEqual(recorder.requests, [])

    def test_non_2xx_raises_notification_error(self):
        notifier = WebhookNotifier("https://example.test/hook")
        with patch("apiwatch.notify.urlopen", Recorder(500)):
            with self.assertRaises(NotificationError):
                notifier.send([_alert()], {})

    def test_network_error_raises_notification_error(self):
        notifier = WebhookNotifier("https://example.test/hook")
        with patch("apiwatch.notify.urlopen", Recorder(error=OSError("boom"))):
            with self.assertRaises(NotificationError):
                notifier.send([_alert()], {})

    def test_supports_url_override_flag(self):
        self.assertTrue(WebhookNotifier("https://example.test/hook").supports_url_override)


if __name__ == "__main__":
    unittest.main()
