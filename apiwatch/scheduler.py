"""Background polling scheduler.

A single daemon thread periodically runs a full scan. The scheduler is disabled
in tests and can be disabled in deployments with ``DISABLE_SCHEDULER=true``.
"""
import logging
import threading

from .engine import Engine

log = logging.getLogger("apiwatch.scheduler")


class Scheduler:
    def __init__(self, engine: Engine, interval_seconds: int = 3600) -> None:
        self.engine = engine
        self.interval = max(30, int(interval_seconds))
        self._stop = threading.Event()
        self._thread = None

    def start(self, run_immediately: bool = True) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, args=(run_immediately,), daemon=True)
        self._thread.start()

    def _loop(self, run_immediately: bool) -> None:
        if run_immediately:
            self._tick()
        while not self._stop.wait(self.interval):
            self._tick()

    def _tick(self) -> None:
        try:
            result = self.engine.scan_all()
            log.info(
                "scheduled scan: %d sources, %d new alerts, %d errors",
                result.sources_checked,
                result.alerts_created,
                len(result.errors),
            )
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("scheduled scan failed")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
