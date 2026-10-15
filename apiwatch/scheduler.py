"""Background polling scheduler.

Two independent daemon threads drive the engine: one runs a scan on a timer, and
an optional second one flushes digests on the (slower) digest interval. A shared
lock guarantees a digest flush never runs at the same time as a scan against the
same engine. The scheduler is disabled in tests and can be disabled in
deployments with ``DISABLE_SCHEDULER=true``.

Each source can override the global cadence with ``scan_interval_seconds``. The
scheduler remembers when it last scanned every source and only scans a source
once its *effective* interval has elapsed: ``source.scan_interval_seconds`` when
set, otherwise the global ``POLL_INTERVAL_SECONDS``. Sources without an override
therefore keep the original behaviour. Because a source may be due more often
than the global interval, the loop wakes up at the smallest effective interval
among the enabled sources rather than always waiting the global interval.
"""
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from .engine import Engine, ScanResult

MIN_SCAN_INTERVAL_SECONDS = 30
MIN_DIGEST_INTERVAL_SECONDS = 300

log = logging.getLogger("apiwatch.scheduler")


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        interval_seconds: int = 3600,
        digest_interval_seconds: int = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.engine = engine
        self.interval = max(MIN_SCAN_INTERVAL_SECONDS, int(interval_seconds))
        if digest_interval_seconds:
            self.digest_interval = max(
                MIN_DIGEST_INTERVAL_SECONDS, int(digest_interval_seconds)
            )
        else:
            self.digest_interval = None
        self._clock = clock or time.monotonic
        self._last_scan: Dict[int, float] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._digest_thread = None

    # -- cadence helpers --------------------------------------------------- #
    def effective_interval(self, source) -> int:
        """Return the seconds between scans for ``source``.

        A per-source override wins over the global interval; both are floored at
        :data:`MIN_SCAN_INTERVAL_SECONDS` so a tiny value cannot hammer a source.
        """
        interval = source.scan_interval_seconds
        if interval is None:
            return self.interval
        return max(MIN_SCAN_INTERVAL_SECONDS, int(interval))

    def due_sources(self, now: Optional[float] = None) -> List:
        """Return the enabled sources whose effective interval has elapsed.

        A source that has never been scanned is always due, so the first tick
        covers the whole registry.
        """
        now = self._clock() if now is None else now
        due = []
        for source in self.engine.store.list_sources(enabled_only=True):
            if self.engine.is_tripped(source):
                # A quarantined source is not scanned until its breaker expires.
                continue
            last = self._last_scan.get(source.id)
            if last is None or now - last >= self.effective_interval(source):
                due.append(source)
        return due

    def run_pending(self, now: Optional[float] = None) -> ScanResult:
        """Scan every due source and remember when each was scanned."""
        now = self._clock() if now is None else now
        due = self.due_sources(now)
        result = self.engine.scan_sources(due)
        for source in due:
            if source.id is not None:
                self._last_scan[source.id] = now
        return result

    def _sleep_seconds(self) -> float:
        """Wake often enough to honour the smallest enabled effective interval.

        Tripped sources are ignored: their (potentially long) remaining cooldown
        must not stop the loop from waking for the healthy sources.
        """
        intervals = [self.interval]
        for source in self.engine.store.list_sources(enabled_only=True):
            if self.engine.is_tripped(source):
                continue
            intervals.append(self.effective_interval(source))
        return min(intervals)

    # -- lifecycle --------------------------------------------------------- #
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
        while not self._stop.wait(self._sleep_seconds()):
            self._tick()

    def _digest_loop(self) -> None:
        while not self._stop.wait(self.digest_interval):
            self._digest_tick()

    def _tick(self) -> None:
        try:
            with self._lock:
                result = self.run_pending()
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
