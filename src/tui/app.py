from __future__ import annotations

import asyncio
import queue
import subprocess
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Footer, Header, ProgressBar, RichLog, Static

from ..config import APP_NAME, Config
from ..database import Database
from ..events import Event, Events, log_setup
from ..service import Service
from .screens import ConfirmRefresh, LogHistory, SettingsScreen


class Snapshot(Message):
    def __init__(self, data: dict):
        super().__init__()
        self.data = data


class SavedLogs(Message):
    def __init__(self, text: str):
        super().__init__()
        self.text = text


class MirrorApp(App[int]):
    TITLE = APP_NAME
    SUB_TITLE = "USB archive"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("s", "sync", "Sync"),
        Binding("r", "retry_connection", "Retry"),
        Binding("f", "force_refresh", "Force refresh", show=False),
        Binding("p", "settings", "Settings"),
        Binding("l", "logs", "Saved logs"),
        Binding("o", "open_mirror", "Open folder", show=False),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]
    CSS = """
    Screen { background: $background; }
    #body { padding: 0 2; }
    #identity { height: auto; margin-top: 1; }
    #connection { text-style: bold; color: $accent; height: auto; }
    #connection.connected { color: $success; }
    #connection.disconnected { color: $text-muted; }
    #connection.error { color: $error; }
    #archive-path, #schedule { color: $text-muted; height: auto; }
    #stats, #counts { height: auto; }
    #activity { height: auto; margin-top: 1; }
    #operation { height: auto; }
    #document-progress, #transfer-progress { height: 1; margin: 0; }
    #document-progress Bar, #transfer-progress Bar { width: 1fr; }
    #documents, #bytes { height: auto; color: $text-muted; }
    #controls { height: 3; margin-top: 0; layout: grid; grid-size: 3; grid-gutter: 0 1; }
    #controls Button { width: 100%; min-width: 12; }
    #live-log { min-height: 8; height: 1fr; border: round $panel; margin-top: 1; }
    .dialog-title { text-style: bold; color: $accent; height: auto; margin-bottom: 1; }
    """

    def __init__(self, config: Config, data_dir: Path, *, start_service: bool = True):
        super().__init__()
        self.config, self.data_dir = config, data_dir
        self.start_service = start_service
        self.event_queue: queue.SimpleQueue[Event] = queue.SimpleQueue()
        self.events = Events(self.event_queue.put, log_setup(data_dir))
        self.service = Service(config, data_dir, self.events)
        self.quitting = False
        self.busy = False
        self.checking_connection = False
        self.settings_pending = False
        self.last_success: float | None = None
        self.next_sync: float | None = None
        self.theme = "textual-dark"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id="body"):
            with Vertical(id="identity"):
                self.connection = Static("Checking connection…", id="connection", markup=False)
                yield self.connection
                self.archive = Static(self.config.mirror_dir, id="archive-path", markup=False)
                yield self.archive
                self.schedule = Static(
                    "Last sync: Never   ·   Next: On connection", id="schedule", markup=False
                )
                yield self.schedule
                self.stats = Static(
                    "Archived: 0   ·   Retained missing: 0", id="stats", markup=False
                )
                yield self.stats
                self.counts = Static(
                    "New: 0   Updated: 0   Unchanged: 0   Errors: 0", id="counts", markup=False
                )
                yield self.counts
            with Vertical(id="activity"):
                self.operation = Static("Waiting for a tablet", id="operation", markup=False)
                yield self.operation
                self.documents = Static("0 documents processed", id="documents", markup=False)
                yield self.documents
                self.document_progress = ProgressBar(
                    total=1, show_eta=False, show_percentage=False, id="document-progress"
                )
                yield self.document_progress
                self.byte_label = Static("", id="bytes", markup=False)
                yield self.byte_label
                self.transfer_progress = ProgressBar(
                    total=None, show_eta=False, show_percentage=False, id="transfer-progress"
                )
                yield self.transfer_progress
            with Horizontal(id="controls"):
                self.sync_button = Button("Sync", id="sync", variant="primary")
                self.retry_button = Button("Retry connection", id="retry")
                self.settings_button = Button("Settings", id="settings")
                yield self.sync_button
                yield self.retry_button
                yield self.settings_button
            self.live_log = RichLog(
                id="live-log", max_lines=2000, wrap=True, markup=False, highlight=False
            )
            self.live_log.border_title = "Live activity"
            yield self.live_log
        yield Footer()

    def on_mount(self) -> None:
        self.show_progress(False)
        self.set_interval(0.05, self.drain_events)
        self.load_snapshot()
        if self.start_service:
            self.service.scheduler.start()

    async def on_unmount(self) -> None:
        # Also handles terminal closure or unexpected UI exit. No UI thread waits on I/O.
        self.service.stop()
        thread = self.service.scheduler.thread
        if thread:
            await asyncio.to_thread(thread.join, self.config.read_timeout + 10)

    def drain_events(self) -> None:
        for _ in range(200):
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self.handle_event(event)
        if self.quitting:
            thread = self.service.scheduler.thread
            if thread is None or not thread.is_alive():
                self.exit(0)

    def handle_event(self, event: Event) -> None:
        if event.kind not in ("bytes", "progress", "schedule"):
            self.live_log.write(event.line())
        data = event.data
        if event.kind == "connection":
            state = data["status"]
            self.connection.set_classes(state)
            self.connection.update(
                "Checking connection…" if state == "connecting" else state.title()
            )
            self.checking_connection = state == "connecting"
            self.update_controls()
            if not self.busy:
                self.operation.update(
                    "Ready"
                    if state == "connected"
                    else event.message
                    if state == "connecting"
                    else "Check the USB cable and web interface setting, then retry."
                )
        elif event.kind == "busy":
            self.busy = data["active"]
            self.update_controls()
            self.show_progress(self.busy)
            if self.busy:
                self.operation.update("Synchronizing…")
                self.document_progress.update(total=1, progress=0)
                self.documents.update("Scanning documents…")
                self.counts.update("New: 0   Updated: 0   Unchanged: 0   Errors: 0")
            else:
                self.transfer_progress.display = False
        elif event.kind in ("scan", "pdf", "down"):
            self.operation.update(event.message)
            self.byte_label.update(
                "Generating PDF…" if event.kind == "pdf" else "Waiting for data…"
            )
            self.transfer_progress.update(total=None, progress=0)
            self.transfer_progress.display = True
        elif event.kind == "bytes":
            received, total = data["received"], data["total"]
            self.transfer_progress.update(total=total, progress=received)
            self.byte_label.update(
                f"{received / 1024**2:.1f} MiB received"
                + (f" / {total / 1024**2:.1f} MiB" if total else "")
            )
        elif event.kind == "progress":
            self.document_progress.update(total=max(1, data["total"]), progress=data["processed"])
            self.documents.update(f"{data['processed']} / {data['total']} documents")
            self.counts.update(
                "   ".join(
                    f"{key.title()}: {data[key]}"
                    for key in ("new", "updated", "unchanged", "errors")
                )
            )
        elif event.kind == "done":
            self.operation.update(
                f"Sync complete · {data['total']} documents · {data['errors']} errors"
            )
            self.show_progress(False)
            self.transfer_progress.display = False
            self.byte_label.update("")
            self.load_snapshot()
        elif event.kind == "schedule":
            self.next_sync = data["next_sync"]
            self.update_schedule()
        elif event.kind == "settings":
            self.config = Config(**data["config"])
            self.archive.update(self.config.mirror_dir)
            self.settings_pending = False
            self.update_controls()
            self.notify("Settings saved")
        elif event.kind == "error":
            self.operation.update(event.message)
            self.transfer_progress.display = False
            self.notify(event.message, severity="error", timeout=8)
        elif event.kind == "settings_error":
            self.settings_pending = False
            self.update_controls()
            self.notify(event.message, severity="error", timeout=8)
        if self.quitting:
            self.operation.update("Stopping safely… waiting for the current USB request")

    def show_progress(self, active: bool) -> None:
        for widget in (self.counts, self.documents, self.document_progress, self.byte_label):
            widget.display = active
        if not active:
            self.transfer_progress.display = False

    def update_controls(self) -> None:
        self.sync_button.disabled = self.busy or self.quitting
        self.retry_button.disabled = self.busy or self.quitting or self.checking_connection
        self.settings_button.disabled = self.busy or self.quitting or self.settings_pending

    @staticmethod
    def format_time(timestamp: float | None, fallback: str) -> str:
        return (
            datetime.fromtimestamp(timestamp).astimezone().strftime("%d %b %H:%M")
            if timestamp
            else fallback
        )

    def update_schedule(self) -> None:
        self.schedule.update(
            f"Last sync: {self.format_time(self.last_success, 'Never')}   ·   Next: {self.format_time(self.next_sync, 'On connection')}"
        )

    @work(thread=True, exclusive=True, group="snapshot")
    def load_snapshot(self) -> None:
        try:
            db = Database(self.data_dir)
            try:
                data = {
                    "archived": len({a["doc_id"] for a in db.rows("artifacts")}),
                    "missing": sum(
                        n["status"] == "missing_from_device" and not n["folder"]
                        for n in db.rows("nodes")
                    ),
                    "last_success": db.meta("last_success"),
                }
            finally:
                db.close()
            self.post_message(Snapshot(data))
        except Exception as exc:
            self.event_queue.put(Event("error", f"Could not read archive status: {exc}"))

    def on_snapshot(self, message: Snapshot) -> None:
        self.stats.update(
            f"Archived: {message.data['archived']}   ·   Retained missing: {message.data['missing']}"
        )
        self.last_success = (
            float(message.data["last_success"]) if message.data["last_success"] else None
        )
        self.update_schedule()

    def action_sync(self) -> None:
        if not self.quitting:
            self.service.scheduler.request()

    def action_retry_connection(self) -> None:
        if not self.busy and not self.quitting and not self.checking_connection:
            if self.service.scheduler.retry_connection():
                self.checking_connection = True
                self.connection.update("Checking connection…")
                self.connection.set_classes("connecting")
                self.update_controls()

    def action_force_refresh(self) -> None:
        if not self.busy and not self.quitting and len(self.screen_stack) == 1:
            self.push_screen(ConfirmRefresh(), self.confirm_refresh)

    def confirm_refresh(self, confirmed: bool) -> None:
        if confirmed and not self.quitting:
            self.service.scheduler.request(force=True)

    def action_settings(self) -> None:
        if (
            not self.busy
            and not self.quitting
            and not self.settings_pending
            and len(self.screen_stack) == 1
        ):
            self.push_screen(SettingsScreen(self.config), self.save_settings)

    def save_settings(self, config: Config | None) -> None:
        if config is not None and not self.quitting:
            self.settings_pending = True
            self.update_controls()
            self.service.configure(config)
            self.notify("Applying settings in the background…")

    def action_logs(self) -> None:
        if len(self.screen_stack) == 1 and not self.quitting:
            self.read_logs()

    @work(thread=True, exclusive=True, group="logs")
    def read_logs(self) -> None:
        lines: deque[str] = deque(maxlen=2000)
        try:
            for index in range(5, -1, -1):
                path = self.data_dir / "logs" / (f"mirror.log.{index}" if index else "mirror.log")
                if path.exists():
                    with path.open(errors="replace") as stream:
                        lines.extend(stream)
            self.post_message(SavedLogs("".join(lines) or "No saved logs yet."))
        except OSError as exc:
            self.event_queue.put(Event("error", f"Could not read logs: {exc}"))

    def on_saved_logs(self, message: SavedLogs) -> None:
        if len(self.screen_stack) == 1 and not self.quitting:
            self.push_screen(LogHistory(message.text))

    @work(thread=True, exclusive=True, group="open-folder")
    def action_open_mirror(self) -> None:
        path = Path(self.config.mirror_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            command = ["open" if sys.platform == "darwin" else "xdg-open", str(path)]
            subprocess.run(command, check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            self.event_queue.put(Event("error", f"Could not open {path}: {exc}"))

    async def action_quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        self.service.stop()
        self.update_controls()
        self.operation.update("Stopping safely… waiting for the current USB request")
        self.drain_events()

    @on(Button.Pressed)
    async def control(self, event: Button.Pressed) -> None:
        actions = {
            "sync": self.action_sync,
            "retry": self.action_retry_connection,
            "force": self.action_force_refresh,
            "settings": self.action_settings,
            "logs": self.action_logs,
            "open-folder": self.action_open_mirror,
        }
        if event.button.id == "quit":
            await self.action_quit()
        elif event.button.id in actions:
            actions[event.button.id]()
