import unittest

from apiwatch.config import DIGEST_INTERVAL_MIN_SECONDS, Config


class DigestConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = Config.from_env({})
        self.assertEqual(config.digest_mode, "off")
        self.assertEqual(config.digest_interval_seconds, 86400)
        self.assertEqual(config.digest_max_alerts, 50)

    def test_valid_modes(self):
        for mode in ("off", "digest", "both"):
            config = Config.from_env({"DIGEST_MODE": mode})
            self.assertEqual(config.digest_mode, mode)

    def test_mode_is_case_insensitive(self):
        config = Config.from_env({"DIGEST_MODE": "DIGEST"})
        self.assertEqual(config.digest_mode, "digest")

    def test_invalid_mode_falls_back_off(self):
        config = Config.from_env({"DIGEST_MODE": "sometimes"})
        self.assertEqual(config.digest_mode, "off")

    def test_interval_minimum_clamped(self):
        config = Config.from_env({"DIGEST_INTERVAL_SECONDS": "10"})
        self.assertEqual(config.digest_interval_seconds, DIGEST_INTERVAL_MIN_SECONDS)

    def test_interval_respected_when_above_minimum(self):
        config = Config.from_env({"DIGEST_INTERVAL_SECONDS": "7200"})
        self.assertEqual(config.digest_interval_seconds, 7200)

    def test_max_alerts_parsed_and_floored(self):
        self.assertEqual(Config.from_env({"DIGEST_MAX_ALERTS": "5"}).digest_max_alerts, 5)
        self.assertEqual(Config.from_env({"DIGEST_MAX_ALERTS": "0"}).digest_max_alerts, 1)


class RetentionConfigTests(unittest.TestCase):
    def test_defaults_disabled(self):
        config = Config.from_env({})
        self.assertEqual(config.retention_days, 0)
        self.assertEqual(config.maintenance_interval_seconds, 86400)

    def test_reads_retention_days(self):
        config = Config.from_env({"RETENTION_DAYS": "14"})
        self.assertEqual(config.retention_days, 14)

    def test_negative_retention_days_floored(self):
        config = Config.from_env({"RETENTION_DAYS": "-5"})
        self.assertEqual(config.retention_days, 0)

    def test_maintenance_interval_floor(self):
        config = Config.from_env({"MAINTENANCE_INTERVAL_SECONDS": "10"})
        self.assertEqual(config.maintenance_interval_seconds, 300)


class WebhookConfigTests(unittest.TestCase):
    def test_defaults_to_none(self):
        self.assertIsNone(Config.from_env({}).webhook_url)

    def test_reads_webhook_url(self):
        config = Config.from_env({"WEBHOOK_URL": "https://example.test/hook"})
        self.assertEqual(config.webhook_url, "https://example.test/hook")

    def test_notify_channels_accepts_webhook(self):
        config = Config.from_env({"NOTIFY_CHANNELS": "webhook"})
        self.assertEqual(config.notify_channels, ["webhook"])


if __name__ == "__main__":
    unittest.main()
