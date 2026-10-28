from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .events import Events


class Scheduler:
    """A single worker serializes probes and syncs; UI requests never block."""

    def __init__(
        self,
        probe: Callable,
        sync: Callable,
        events: Events,
        interval: int = 900,
        poll: int = 10,
        clock: Callable = time.time,
    ):
        self.probe, self.sync, self.events = probe, sync, events
        self.interval, self.poll, self.clock = interval, poll, clock
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.guard = threading.Lock()
        self.running = False
        self.requested: bool | None = None
        self.retry_requested = False
        self.connected = False
        self.next_sync = 0.0
        self.previous_tick = self.clock()
        self.last_state: str | None = None
        self.thread: threading.Thread | None = None

    def request(self, force: bool = False) -> bool:
        with self.guard:
            if self.running or self.requested is not None:
                self.events.emit("info", "Synchronization is already running or scheduled")
                return False
            self.requested = force
        self.wake_event.set()
        return True

    def retry_connection(self) -> bool:
        """Wake the existing worker for a probe; never force a content refresh."""
        with self.guard:
            if (
                self.stop_event.is_set()
                or self.running
                or self.requested is not None
                or self.retry_requested
            ):
                self.events.emit(
                    "info", "A connection check or synchronization is already running or scheduled"
                )
                return False
            self.retry_requested = True
        self.wake_event.set()
        return True

    def tick(self) -> None:
        now = self.clock()
        # A wall-clock gap (including macOS sleep) invalidates cached connectivity.
        if now - self.previous_tick > max(60, self.poll * 3) or now < self.previous_tick:
            self.connected = False
            self.next_sync = 0
            self.events.emit("info", "Wake or clock change detected; rechecking USB connection")
        self.previous_tick = now
        with self.guard:
            if self.running:
                return
            manual, self.requested = self.requested, None
            retry, self.retry_requested = self.retry_requested, False
            self.running = True
        try:
            if retry or self.last_state is None:
                self.events.emit("connection", "Checking USB connection…", status="connecting")
            state, detail = self.probe()
            if retry or state != self.last_state:
                self.events.emit("connection", detail, status=state)
                self.last_state = state
            newly_connected = state == "connected" and not self.connected
            self.connected = state == "connected"
            if self.connected and (manual is not None or newly_connected or now >= self.next_sync):
                self.events.emit("busy", "Synchronizing", active=True)
                try:
                    self.sync(bool(manual))
                finally:
                    self.next_sync = self.clock() + self.interval
                    self.events.emit("busy", "Idle", active=False)
            self.events.emit(
                "schedule",
                "Next synchronization",
                next_sync=self.next_sync if self.connected else None,
            )
        except Exception as exc:
            self.connected = False
            self.next_sync = self.clock() + self.poll
            self.last_state = "error"
            self.events.emit("connection", str(exc), status="error")
            self.events.emit("error", str(exc))
        finally:
            self.previous_tick = self.clock()
            with self.guard:
                self.running = False

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            self.wake_event.clear()
            self.tick()
            self.wake_event.wait(self.poll)
        self.events.emit("stopped", "Background worker stopped")

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="USB Mirror worker", daemon=False)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
