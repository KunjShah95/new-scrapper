"""
Scheduler: run repeated scrape cycles, concurrently, forever.

Each cycle sweeps `district_order` one district at a time, fully finishing one
before starting the next (e.g. all of Ahmedabad, then Gandhinagar, then
Mehsana) - see `gujrera.pipeline.run_districts`. Within a district every
ongoing project is discovered fresh and scraped concurrently (bounded by
--project-workers), each project itself fetching its tabs and downloading its
documents concurrently too. After the whole sweep, the scheduler sleeps until
the next interval and repeats.

A project already fully scraped in a previous cycle is still re-scraped —
GujRERA content (progress %, quarterly filings, documents) changes under a
project without changing its id, so the only way to catch new content is to
re-pull it. Set --interval high enough to be polite to the upstream site.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from .pipeline import run_districts

log = logging.getLogger("gujrera.scheduler")

DEFAULT_DISTRICT_ORDER = ["ahmedabad", "gandhinagar", "mehsana"]


class Scheduler:
    """Runs scrape cycles on a fixed interval until stopped."""

    def __init__(self, output_root: Path, interval: float = 3600.0,
                 project_workers: int = 4, endpoint_workers: int = 8, doc_workers: int = 8,
                 download_files: bool = True, limit: Optional[int] = None,
                 district_order: Optional[List[str]] = None):
        self.output_root = output_root
        self.interval = interval
        self.project_workers = project_workers
        self.endpoint_workers = endpoint_workers
        self.doc_workers = doc_workers
        self.download_files = download_files
        self.limit = limit
        self.district_order = district_order or list(DEFAULT_DISTRICT_ORDER)
        self._stop = threading.Event()

    def stop(self, *_args) -> None:
        log.info("Stop requested — finishing in-flight work, no new cycle will start.")
        self._stop.set()

    def run_forever(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        try:
            signal.signal(signal.SIGTERM, self.stop)
        except (ValueError, AttributeError):
            pass  # not available on every platform/thread

        cycle = 0
        while not self._stop.is_set():
            cycle += 1
            started = time.monotonic()
            log.info("=== cycle %d starting ===", cycle)
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the scheduler
                log.exception("cycle %d failed", cycle)
            elapsed = time.monotonic() - started
            log.info("=== cycle %d done in %.1fs ===", cycle, elapsed)
            remaining = self.interval - elapsed
            if remaining > 0 and not self._stop.is_set():
                log.info("sleeping %.0fs until next cycle", remaining)
                self._stop.wait(remaining)

    def run_once(self) -> Dict[str, List[dict]]:
        log.info("sweeping districts in order: %s", " -> ".join(self.district_order))
        return run_districts(
            self.district_order, self.output_root, download_files=self.download_files,
            limit=self.limit, project_workers=self.project_workers,
            endpoint_workers=self.endpoint_workers, doc_workers=self.doc_workers,
            stop_check=self._stop.is_set,
        )
