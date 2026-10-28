from __future__ import annotations

from dataclasses import asdict

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RichLog, Static

from ..config import Config


class ConfirmRefresh(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmRefresh { align: center middle; background: $background 75%; }
    ConfirmRefresh > Vertical { width: 64; max-width: 95%; height: auto;
        padding: 1 2; border: round $warning; background: $surface; }
    ConfirmRefresh Static { margin-bottom: 1; height: auto; }
    ConfirmRefresh Horizontal { height: 3; align-horizontal: right; }
    ConfirmRefresh Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Refresh every document?", classes="dialog-title")
            yield Static(
                "This exports every selected format again. Large notebooks may take a long time.",
                markup=False,
            )
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Refresh all", id="confirm", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class SettingsScreen(ModalScreen[Config | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; background: $background 75%; }
    SettingsScreen > Vertical { width: 78; max-width: 98%; height: 90%;
        border: round $primary; background: $surface; padding: 1 2; }
    SettingsScreen VerticalScroll { height: 1fr; }
    SettingsScreen Label { margin-top: 1; }
    SettingsScreen Input { margin-bottom: 0; }
    SettingsScreen .formats { height: auto; }
    SettingsScreen #settings-error { color: $error; height: auto; }
    SettingsScreen .hint { color: $text-muted; height: auto; }
    SettingsScreen #settings-actions { height: 3; align-horizontal: right; margin-top: 1; }
    SettingsScreen Button { margin-left: 1; }
    """

    def __init__(self, config: Config):
        super().__init__()
        self.original = config

    def compose(self) -> ComposeResult:
        c = self.original
        with Vertical():
            yield Static("Settings", classes="dialog-title")
            with VerticalScroll():
                yield Label("Archive directory")
                yield Input(
                    c.mirror_dir, id="directory", placeholder="~/Documents/reMarkable Mirror"
                )
                yield Static(
                    "Changing directories copies the archive and keeps the originals.",
                    classes="hint",
                    markup=False,
                )
                yield Label("USB address")
                yield Input(c.address, id="address")
                yield Label("Sync interval (minutes)")
                yield Input(str(c.interval_seconds / 60), id="interval", type="number")
                yield Label("Full refresh interval (days)")
                yield Input(str(c.refresh_days), id="refresh-days", type="number")
                yield Label("Export formats")
                with Horizontal(classes="formats"):
                    yield Checkbox("PDF", "pdf" in c.formats, id="pdf")
                    yield Checkbox("RMDOC", "rmdoc" in c.formats, id="rmdoc")
                yield Checkbox("Keep previous tablet revisions", c.history, id="history")
                yield Checkbox("Log unchanged documents", c.verbose, id="verbose")
                yield Checkbox("Open in Terminal at login (macOS)", c.launch_at_login, id="login")
                yield Static(
                    "Login startup opens a terminal window. Keep this session open for automatic sync.",
                    classes="hint",
                    markup=False,
                )
                yield Static("", id="settings-error", markup=False)
            with Horizontal(id="settings-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", id="save", variant="primary")

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        values = asdict(self.original)
        try:
            values.update(
                mirror_dir=self.query_one("#directory", Input).value,
                address=self.query_one("#address", Input).value,
                interval_seconds=int(float(self.query_one("#interval", Input).value) * 60),
                refresh_days=float(self.query_one("#refresh-days", Input).value),
                formats=[
                    fmt for fmt in ("rmdoc", "pdf") if self.query_one(f"#{fmt}", Checkbox).value
                ],
                history=self.query_one("#history", Checkbox).value,
                verbose=self.query_one("#verbose", Checkbox).value,
                launch_at_login=self.query_one("#login", Checkbox).value,
            )
            config = Config(**values)
            config.validate()
        except (ValueError, OverflowError) as exc:
            self.query_one("#settings-error", Static).update(str(exc))
            return
        self.dismiss(config)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class LogHistory(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close")]
    DEFAULT_CSS = """
    LogHistory { align: center middle; background: $background 75%; }
    LogHistory > Vertical { width: 95%; height: 90%; padding: 1 2;
        border: round $primary; background: $surface; }
    LogHistory RichLog { height: 1fr; }
    LogHistory Button { align-horizontal: right; }
    """

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Saved logs · latest 2,000 lines", classes="dialog-title")
            yield RichLog(id="history-log", wrap=True, markup=False, highlight=False)
            yield Button("Close", id="close")

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        for line in self.text.splitlines():
            log.write(line)
        log.focus()

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)
