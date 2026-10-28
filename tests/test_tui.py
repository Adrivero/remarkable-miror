import asyncio
import threading

import pytest
from textual.widgets import Checkbox, Input, RichLog, Static

from src import main
from src.events import Event
from src.filesystem import interface_lock
from src.models import MirrorError
from src.tui.app import MirrorApp
from src.tui.screens import ConfirmRefresh, LogHistory, SettingsScreen


@pytest.mark.parametrize("size", [(120, 40), (80, 24), (60, 20)])
async def test_dashboard_keyboard_and_resize(h, size):
    app = MirrorApp(h.config, h.data, start_service=False)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert app.query_one("#live-log", RichLog).is_mounted
        await pilot.press("p")
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")
        await pilot.press("f")
        assert isinstance(app.screen, ConfirmRefresh)
        await pilot.press("escape")
        assert app.service.scheduler.requested is None
        await pilot.press("s")
        assert app.service.scheduler.requested is False
        await pilot.press("q")
        assert app.quitting


async def test_force_refresh_requires_confirmation(h):
    app = MirrorApp(h.config, h.data, start_service=False)
    async with app.run_test() as pilot:
        await pilot.press("f")
        await pilot.click("#cancel")
        assert app.service.scheduler.requested is None
        await pilot.press("f")
        await pilot.click("#confirm")
        assert app.service.scheduler.requested is True


async def test_settings_validate_and_queue_without_blocking(h):
    app = MirrorApp(h.config, h.data, start_service=False)
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("p")
        screen = app.screen
        screen.query_one("#pdf", Checkbox).value = False
        screen.query_one("#rmdoc", Checkbox).value = False
        screen.save()
        assert "Select PDF" in str(screen.query_one("#settings-error", Static).content)
        screen.query_one("#pdf", Checkbox).value = True
        screen.query_one("#interval", Input).value = "0"
        screen.save()
        assert "Interval" in str(screen.query_one("#settings-error", Static).content)
        screen.query_one("#interval", Input).value = "7"
        screen.query_one("#refresh-days", Input).value = "3"
        screen.save()
        await pilot.pause()
        pending = app.service.pending_config
        assert pending.formats == ["pdf"]
        assert pending.interval_seconds == 420 and pending.refresh_days == 3
        assert app.settings_pending
        app.handle_event(Event("settings_error", "Disk is full"))
        assert not app.settings_pending and not app.settings_button.disabled


async def test_terminal_handles_untrusted_names_and_real_progress(h):
    app = MirrorApp(h.config, h.data, start_service=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.handle_event(Event("pdf", "Exporting [bold]notes[/bold].pdf"))
        assert app.transfer_progress.total is None
        assert "[bold]" in str(app.operation.content)
        app.handle_event(Event("bytes", "Receiving", {"received": 512, "total": 1024}))
        assert app.transfer_progress.progress == 512 and app.transfer_progress.total == 1024
        app.handle_event(Event("bytes", "Receiving", {"received": 600, "total": None}))
        assert app.transfer_progress.total is None
        app.handle_event(
            Event(
                "progress",
                "Processed",
                {"processed": 2, "total": 3, "new": 2, "updated": 0, "unchanged": 0, "errors": 0},
            )
        )
        assert app.document_progress.progress == 2 and app.document_progress.total == 3
        await pilot.press("p")
        # Background events must work while the settings screen is open.
        app.handle_event(Event("connection", "USB ready", {"status": "connected"}))
        assert "Connected" in str(app.connection.content)
        assert isinstance(app.screen, SettingsScreen)


async def test_saved_logs_in_terminal(h):
    app = MirrorApp(h.config, h.data, start_service=False)
    app.events.emit("info", "previous saved activity")
    async with app.run_test() as pilot:
        await pilot.press("l")
        for _ in range(30):
            await pilot.pause(0.02)
            if isinstance(app.screen, LogHistory):
                break
        assert isinstance(app.screen, LogHistory)
        assert "previous saved activity" in app.screen.text
        await pilot.press("escape")
        assert len(app.screen_stack) == 1


async def test_terminal_responsive_and_graceful_quit_during_download(h):
    entered, release = threading.Event(), threading.Event()
    app = MirrorApp(h.config, h.data, start_service=False)

    def slow_sync(force):
        entered.set()
        release.wait(5)
        app.events.emit("info", "Transfer finished")

    app.service.scheduler.probe = lambda: ("connected", "USB ready")
    app.service.scheduler.sync = slow_sync
    try:
        async with app.run_test() as pilot:
            app.service.scheduler.start()
            assert await asyncio.to_thread(entered.wait, 2)
            await pilot.pause(0.1)
            assert app.sync_button.disabled
            await pilot.press("l")
            for _ in range(30):
                await pilot.pause(0.02)
                if isinstance(app.screen, LogHistory):
                    break
            assert isinstance(app.screen, LogHistory)
            await pilot.press("escape")
            await pilot.press("q")
            assert app.quitting
            assert app.service.cancel.is_set()
            assert app.service.scheduler.thread.is_alive()
            assert not app.service.scheduler.request()
            release.set()
            await pilot.pause(0.2)
    finally:
        release.set()
        app.service.stop()
        await asyncio.to_thread(app.service.scheduler.thread.join, 5)
    assert not app.service.scheduler.thread.is_alive()


async def test_real_service_to_tui_bridge(h):
    app = MirrorApp(h.config, h.data, start_service=False)
    h.client.events = app.events
    app.service.scheduler.probe = h.client.probe
    # Real engine with offline tablet transport, using the same event queue as production.
    from src.sync_engine import SyncEngine

    app.service.scheduler.sync = lambda force: SyncEngine(
        h.config, h.data, app.events, h.client
    ).run(force=force)
    async with app.run_test() as pilot:
        app.service.scheduler.start()
        for _ in range(100):
            await pilot.pause(0.02)
            if "Archived: 1" in str(app.stats.content):
                break
        assert "Connected" in str(app.connection.content)
        assert "Archived: 1" in str(app.stats.content)
        assert "1 / 1" in str(app.documents.content)
        assert not app.sync_button.disabled
        await pilot.press("q")


def test_tui_session_lock_does_not_block_engine(h):
    with interface_lock(h.data):
        with pytest.raises(MirrorError, match="already running"):
            with interface_lock(h.data):
                pass
        assert h.sync().complete


def test_default_launch_is_tui_and_no_legacy_gui_command(h, monkeypatch):
    h.config.save(h.data)
    calls = []
    monkeypatch.setattr(main, "run_tui", lambda *args: calls.append(args) or 0)
    assert main.main(["--data-dir", str(h.data)]) == 0
    assert main.main(["--data-dir", str(h.data), "tui"]) == 0
    assert len(calls) == 2
    with pytest.raises(SystemExit):
        main.parser().parse_args(["gui"])


def test_tui_requires_terminal(h, monkeypatch, capsys):
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: False)
    assert main.main(["--data-dir", str(h.data), "tui"]) == 1
    assert "interactive terminal" in capsys.readouterr().err


async def test_compact_dashboard_has_three_actions_and_room_for_logs(h):
    from textual.widgets import Button

    app = MirrorApp(h.config, h.data, start_service=False)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert {b.id for b in app.query_one("#controls").query(Button)} == {
            "sync",
            "retry",
            "settings",
        }
        assert not app.counts.display and not app.document_progress.display
        assert app.live_log.size.height >= 8
        assert app.live_log.region.bottom <= 23


@pytest.mark.parametrize("use_button", [True, False])
async def test_retry_button_and_key_wake_worker_without_blocking_ui(h, use_button):
    app = MirrorApp(h.config, h.data, start_service=False)
    app.service.scheduler.poll = 3600
    entered, release = threading.Event(), threading.Event()
    calls = []

    def probe():
        calls.append(1)
        if len(calls) > 1:
            entered.set()
            release.wait(5)
        return "disconnected", "USB API unavailable"

    app.service.scheduler.probe = probe
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            app.service.scheduler.start()
            for _ in range(50):
                await pilot.pause(0.02)
                if str(app.connection.content) == "Disconnected":
                    break
            assert str(app.connection.content) == "Disconnected"
            if use_button:
                await pilot.click("#retry")
            else:
                await pilot.press("r")
            assert await asyncio.to_thread(entered.wait, 1)
            await pilot.pause(0.1)
            assert app.retry_button.disabled
            assert "Checking" in str(app.connection.content)
            await pilot.press("r")
            assert len(calls) == 2
            release.set()
            for _ in range(50):
                await pilot.pause(0.02)
                if not app.retry_button.disabled:
                    break
            assert str(app.connection.content) == "Disconnected"
            assert not app.retry_button.disabled
            assert not app.service.scheduler.requested  # No forced refresh requested.
            await pilot.press("q")
    finally:
        release.set()
        app.service.stop()
        await asyncio.to_thread(app.service.scheduler.thread.join, 5)
