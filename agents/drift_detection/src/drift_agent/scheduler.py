"""Periodic checks. In the demo each tick simulates a new production batch and checks it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler

if TYPE_CHECKING:
    from drift_agent.service import DriftService

log = logging.getLogger("drift_agent.scheduler")
JOB_ID = "drift-check"


class LiveRunner:
    """Background scheduler that can be started and stopped from the API."""

    def __init__(self, service: "DriftService"):
        self.service = service
        self.scheduler = BackgroundScheduler(daemon=True)
        self.scheduler.start()
        self.interval = int(service.settings["scheduler"]["interval_seconds"])
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self.scheduler.get_job(JOB_ID) is not None

    def start(self, interval: int | None = None) -> None:
        if interval:
            self.interval = int(interval)
        self.scheduler.add_job(self._tick, "interval", seconds=self.interval, id=JOB_ID,
                               replace_existing=True, max_instances=1, coalesce=True)
        self.service.store.audit("system", "live_mode_started", {"interval_seconds": self.interval})

    def stop(self) -> None:
        if self.running:
            self.scheduler.remove_job(JOB_ID)
            self.service.store.audit("system", "live_mode_stopped", {})

    def _tick(self) -> None:
        try:
            self.service.step()
            self.last_error = None
        except Exception as exc:  # keep the scheduler alive; surface the error in the UI
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("scheduled check failed")

    def shutdown(self) -> None:
        self.scheduler.shutdown(wait=False)
