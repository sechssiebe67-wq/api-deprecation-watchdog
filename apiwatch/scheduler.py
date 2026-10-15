"""Background polling scheduler.

Two independent daemon threads drive the engine: one runs a full scan on the
poll interval, and an optional second one flushes digests on the (slower)
digest interval. A shared lock guarantees a digest flush never runs at the same
time as a scan against the same engine. The scheduler is disabled in tests and
can be disabled in deployments with ``DISABLE_SCHEDULER=true``.
"""
import logging
import threading

from .engine import Engine

MIN_SCAN_INTERVAL_SECONDS = 30
MIN_DIGEST_INTERVAL_SECONDS = 300

log = logging.getLogger("apiwatch.scheduler")


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        interval_seconds: int = 3600,
        digest_interval_seconds: int = None,
    ) -> None:
        self.engine = engine
        self.interval = max(MIN_SCAN_INTERVAL_SECONDS, int(interval_seconds))
        if digest_interval_seconds:
            self.digest_interval = max(
                MIN_DIGEST_INTERVAL_SECONDS, int(digest_interval_seconds)
            )
        else:
            self.digest_interval = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._digest_thread = None

    def start(self, run_immediately: bool = True) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, args=(run_immediately,), daemon=True)
        self._thread.start()
        if self.digest_interval is not None and self._digest_thread is None:
            self._digest_thread = threading.Thread(target=self._digest_loop, daemon=True)
            self._digest_thread.start()

    def _loop(self, run_immediately: bool) -> None:
        if run_immediately:
            self._tick()
        while not self._stop.wait(self.interval):
            self._tick()

    def _digest_loop(self) -> None:
        while not self._stop.wait(self.digest_interval):
            self._digest_tick()

    def _tick(self) -> None:
        try:
            with self._lock:
                result = self.engine.scan_all()
            log.info(
                "scheduled scan: %d sources, %d new alerts, %d errors",
                result.sources_checked,
                result.alerts_created,
                len(result.errors),
            )
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("scheduled scan failed")

    def _digest_tick(self) -> None:
        try:
            with self._lock:
                digest = self.engine.flush_digest()
            if digest is not None:
                log.info(
                    "scheduled digest: %d alerts (severity=%s, delivered=%s)",
                    digest.alert_count,
                    digest.severity,
                    digest.delivered,
                )
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("scheduled digest flush failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._digest_thread is not None:
            self._digest_thread.join(timeout=5)
            self._digest_thread = None
