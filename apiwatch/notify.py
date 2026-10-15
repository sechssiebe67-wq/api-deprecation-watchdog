"""Notification channels.

Each notifier receives the batch of newly created alerts plus a mapping of
``source_id -> source name`` and decides how to deliver them. Failures are
reported as ``NotificationError`` so the engine can count them without aborting
the scan.
"""
import json
import smtplib
from email.message import EmailMessage
from typing import Dict, List, Sequence
from urllib.request import Request, urlopen

from .models import Alert, Digest

_SEVERITY_EMOJI = {"critical": "\U0001f6d1", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}


class NotificationError(Exception):
    pass


def format_alert(alert: Alert, source_name: str = "") -> str:
    icon = _SEVERITY_EMOJI.get(alert.severity, "")
    lines = ["%s [%s] %s" % (icon, alert.severity.upper(), alert.title or "(untitled)")]
    if source_name:
        lines.append("source: %s" % source_name)
    if alert.reason:
        lines.append(alert.reason)
    if alert.link:
        lines.append(alert.link)
    return "\n".join(lines)


class Notifier:
    name = "base"

    def send(self, alerts: Sequence[Alert], source_names: Dict[int, str]) -> None:
        raise NotImplementedError

    def send_digest(self, digest: Digest, source_names: Dict[int, str]) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, stream=None) -> None:
        import sys

        self.stream = stream or sys.stdout

    def send(self, alerts, source_names) -> None:
        for alert in alerts:
            self.stream.write(format_alert(alert, source_names.get(alert.source_id, "")) + "\n")

    def send_digest(self, digest, source_names) -> None:
        self.stream.write(digest.body)
        if not digest.body.endswith("\n"):
            self.stream.write("\n")


class SlackNotifier(Notifier):
    name = "slack"

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, alerts, source_names) -> None:
        if not alerts:
            return
        text_lines = ["*API Deprecation Watchdog* detected %d new alert(s):" % len(alerts), ""]
        for alert in alerts[:20]:
            text_lines.append(format_alert(alert, source_names.get(alert.source_id, "")))
            text_lines.append("")
        payload = {"text": "\n".join(text_lines)}
        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 (configured webhook)
                if response.status >= 400:
                    raise NotificationError("slack returned %s" % response.status)
        except NotificationError:
            raise
        except Exception as exc:  # network and parsing errors alike
            raise NotificationError("slack delivery failed: %s" % exc) from exc

    def send_digest(self, digest, source_names) -> None:
        if not digest.body:
            return
        text_lines = ["*API Deprecation Watchdog digest*", "", digest.body]
        payload = {"text": "\n".join(text_lines)}
        request = Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 (configured webhook)
                if response.status >= 400:
                    raise NotificationError("slack returned %s" % response.status)
        except NotificationError:
            raise
        except Exception as exc:
            raise NotificationError("slack delivery failed: %s" % exc) from exc


class EmailNotifier(Notifier):
    name = "email"

    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str = None,
        password: str = None,
        sender: str = None,
        recipients: List[str] = None,
        starttls: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender or username
        self.recipients = recipients or []
        self.starttls = starttls

    def send(self, alerts, source_names) -> None:
        if not alerts or not self.recipients:
            return
        message = EmailMessage()
        message["Subject"] = "API Deprecation Watchdog: %d new alert(s)" % len(alerts)
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(
            "\n\n".join(
                format_alert(alert, source_names.get(alert.source_id, "")) for alert in alerts
            )
        )
        self._deliver(message)

    def send_digest(self, digest, source_names) -> None:
        if not self.recipients:
            return
        message = EmailMessage()
        message["Subject"] = digest.subject or "API Deprecation Watchdog digest"
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(digest.body)
        self._deliver(message)

    def _deliver(self, message: EmailMessage) -> None:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as client:
                if self.starttls:
                    client.starttls()
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(message)
        except Exception as exc:
            raise NotificationError("email delivery failed: %s" % exc) from exc
