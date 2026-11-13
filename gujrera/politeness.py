"""Shared limits that keep a long document crawl polite to the portal.

One `Throttle` is shared by every download thread in a run:

  * a hard cap on simultaneous downloads, across all projects;
  * a circuit breaker on transient failures (timeouts, resets, 5xx): too many in
    a short window pauses *every* worker, and each further trip doubles the pause;
  * a stop on refusal: a run of HTTP 403/429 in a row means the portal is pushing
    back, so the run ends cleanly instead of pressing on. Nothing here rotates IPs
    or proxies to get around a refusal — the answer to "no" is to stop and wait.

It is deliberately conservative: a backfill is not urgent, a ban is permanent.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Deque

log = logging.getLogger("gujrera.politeness")


class PortalRefused(RuntimeError):
    """The portal keeps answering 403/429: stop the run."""


def classify(status: str) -> str:
    """'ok' | 'refused' | 'transient' | 'permanent' for a manifest row status."""
    if status == "ok":
        return "ok"
    text = status.lower()
    if "missing on portal" in text or "empty response" in text or "http 404" in text:
        return "permanent"   # the portal answered; the file just is not there
    if "http 403" in text or "http 429" in text or "too many 429" in text:
        return "refused"
    return "transient"


class Throttle:
    def __init__(self, max_concurrent: int = 12, window: float = 120.0,
                 trip_after: int = 8, pause: float = 600.0, max_pause: float = 3600.0,
                 refuse_after: int = 5):
        self._slots = threading.BoundedSemaphore(max(1, max_concurrent))
        self._lock = threading.Lock()
        self._transient: Deque[float] = deque()
        self._window = window
        self._trip_after = trip_after
        self._base_pause = pause
        self._pause = pause
        self._max_pause = max_pause
        self._paused_until = 0.0
        self._refused_streak = 0
        self._refuse_after = refuse_after
        self.refused = False
        self.trips = 0

    def gate(self) -> None:
        """Block while paused; raise once the portal has refused us."""
        while True:
            if self.refused:
                raise PortalRefused("portal refused repeated requests (403/429); run stopped")
            wait = self._paused_until - time.time()
            if wait <= 0:
                return
            time.sleep(min(wait, 30.0))

    @contextmanager
    def slot(self):
        self.gate()
        with self._slots:
            yield

    def record(self, status: str) -> None:
        kind = classify(status)
        now = time.time()
        with self._lock:
            if kind == "refused":
                self._refused_streak += 1
                if self._refused_streak >= self._refuse_after and not self.refused:
                    self.refused = True
                    log.error("portal refused %d requests in a row - stopping the run",
                              self._refused_streak)
                return
            self._refused_streak = 0
            if kind != "transient":
                if kind == "ok" and now > self._paused_until + self._window:
                    self._pause = self._base_pause   # healthy again: reset the backoff
                return
            self._transient.append(now)
            while self._transient and now - self._transient[0] > self._window:
                self._transient.popleft()
            if len(self._transient) >= self._trip_after and now >= self._paused_until:
                self._paused_until = now + self._pause
                self.trips += 1
                log.warning("%d transient failures in %ds - pausing all downloads for %d min",
                            len(self._transient), int(self._window), int(self._pause // 60))
                self._pause = min(self._pause * 2, self._max_pause)
                self._transient.clear()
