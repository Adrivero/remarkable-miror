import threading
from dataclasses import replace

from src.events import Events
from src.scheduler import Scheduler
from src.service import Service


def test_connect_disconnect_reconnect_and_sleep():
    clock = [1000.0]
    state = ["disconnected"]
    syncs, events = [], []
    scheduler = Scheduler(
        lambda: (state[0], "state"),
        lambda force: syncs.append(force),
        Events(events.append),
        clock=lambda: clock[0],
    )
    scheduler.tick()
    assert not syncs
    state[0] = "connected"
    scheduler.tick()
    scheduler.tick()
    assert syncs == [False]
    clock[0] += 100
    scheduler.tick()
    assert syncs == [False, False]
    assert any("Wake" in e.message for e in events)
    state[0] = "disconnected"
    scheduler.tick()
    state[0] = "connected"
    scheduler.tick()
    assert len(syncs) == 3


def test_long_sync_is_not_misclassified_as_sleep():
    clock = [1000.0]
    calls = []

    def sync(force):
        calls.append(force)
        clock[0] += 100

    scheduler = Scheduler(lambda: ("connected", "ok"), sync, Events(), clock=lambda: clock[0])
    scheduler.tick()
    scheduler.tick()
    assert calls == [False]


def test_manual_request_rejected_during_auto_sync():
    entered, release = threading.Event(), threading.Event()

    def sync(force):
        entered.set()
        assert release.wait(5)

    scheduler = Scheduler(lambda: ("connected", "ok"), sync, Events())
    scheduler.start()
    try:
        assert entered.wait(3)
        assert not scheduler.request()
        assert not scheduler.request(force=True)
    finally:
        release.set()
        scheduler.stop()
        scheduler.thread.join(5)
    assert not scheduler.thread.is_alive()


def test_settings_relocation_keeps_old_archive(h):
    h.sync()
    h.config.save(h.data)
    old = h.artifact_path()
    service = Service(h.config, h.data, h.events)
    config = replace(h.config, mirror_dir=str(h.root.parent / "new mirror"))
    service.apply_config(config)
    assert old.exists()
    assert (h.root.parent / "new mirror/Notebook.pdf").read_bytes() == old.read_bytes()
    h.config = config
    h.root = h.root.parent / "new mirror"
    h.tablet.downloads.clear()
    h.sync()
    assert not h.tablet.downloads


def test_migration_does_not_overwrite_other_files(h):
    import pytest

    from src.models import MirrorError

    h.sync()
    service = Service(h.config, h.data, h.events)
    root = h.root.parent / "new"
    root.mkdir()
    (root / "Notebook.pdf").write_text("mine")
    with pytest.raises(MirrorError, match="different file"):
        service.apply_config(replace(h.config, mirror_dir=str(root)))
    assert (root / "Notebook.pdf").read_text() == "mine"


def test_retry_rechecks_unchanged_status_and_does_not_force_sync():
    events, syncs = [], []
    state = ["disconnected"]
    scheduler = Scheduler(lambda: (state[0], "USB state"), syncs.append, Events(events.append))
    scheduler.tick()
    events.clear()
    assert scheduler.retry_connection()
    assert scheduler.wake_event.is_set()
    assert not scheduler.retry_connection()
    scheduler.tick()
    assert [e.data["status"] for e in events if e.kind == "connection"] == [
        "connecting",
        "disconnected",
    ]
    assert not syncs
    state[0] = "connected"
    assert scheduler.retry_connection()
    scheduler.tick()
    assert syncs == [False]  # Normal reconnect sync.
    assert scheduler.retry_connection()
    scheduler.tick()
    assert syncs == [False]  # Already connected: probe only, no forced sync.


def test_retry_rejected_during_work_and_reports_probe_error():
    events = []

    def failure():
        raise OSError("USB failure")

    scheduler = Scheduler(failure, lambda force: None, Events(events.append))
    scheduler.running = True
    assert not scheduler.retry_connection()
    scheduler.running = False
    assert scheduler.retry_connection()
    scheduler.tick()
    assert [e.data["status"] for e in events if e.kind == "connection"] == ["connecting", "error"]
    assert not scheduler.running
    scheduler.stop()
    assert not scheduler.retry_connection()
