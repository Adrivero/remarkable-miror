from __future__ import annotations

import threading
from pathlib import Path

from .config import Config
from .database import Database
from .events import Events
from .filesystem import archive_lock, durable_copy, safe_path, sha256
from .login import set_launch_at_login
from .models import MirrorError
from .scheduler import Scheduler
from .sync_engine import SyncEngine
from .usb_client import USBClient


class Service:
    def __init__(self, config: Config, data_dir: Path, events: Events):
        self.config, self.data_dir, self.events = config, data_dir, events
        self.cancel = threading.Event()
        self.settings_lock = threading.Lock()
        self.pending_config: Config | None = None
        self.scheduler = Scheduler(
            self.probe, self.sync, events, config.interval_seconds, config.poll_seconds
        )

    def probe(self):
        with self.settings_lock:
            pending, self.pending_config = self.pending_config, None
        if pending:
            try:
                self.apply_config(pending)
                self.events.emit("settings", "Settings saved", config=vars(pending))
            except Exception as exc:
                self.events.emit("settings_error", f"Settings were not applied: {exc}")
        client = USBClient(self.config, self.events, cancel=self.cancel)
        try:
            return client.probe()
        finally:
            client.close()

    def sync(self, force: bool):
        return SyncEngine(self.config, self.data_dir, self.events, cancel=self.cancel).run(
            force=force
        )

    def configure(self, config: Config) -> None:
        config.validate()
        with self.settings_lock:
            self.pending_config = config
        self.scheduler.wake_event.set()

    def apply_config(self, config: Config) -> None:
        old_root = Path(self.config.mirror_dir).resolve()
        new_root = Path(config.mirror_dir).resolve()
        if new_root.is_relative_to(
            self.data_dir.resolve()
        ) or self.data_dir.resolve().is_relative_to(new_root):
            raise MirrorError("Mirror and application data directories must be separate")
        if old_root != new_root and (
            new_root.is_relative_to(old_root) or old_root.is_relative_to(new_root)
        ):
            raise MirrorError("Choose a new mirror outside the existing mirror directory")
        with archive_lock(self.data_dir, old_root):
            db = Database(self.data_dir)
            try:
                if db.rows("journal"):
                    raise MirrorError(
                        "Run a sync to recover interrupted updates before changing settings"
                    )
                bound = db.meta("mirror_root")
                if bound and Path(bound) != old_root and Path(bound) != new_root:
                    raise MirrorError(
                        "Restore the database's archive directory in Settings before moving it"
                    )
                if old_root != new_root and (not bound or Path(bound) != new_root):
                    with archive_lock(new_root):
                        self.events.emit(
                            "info",
                            "Copying archive to the selected directory; original files will be retained",
                        )
                        for artifact in db.rows("artifacts"):
                            if self.cancel.is_set():
                                raise MirrorError("Directory change cancelled")
                            source = safe_path(old_root, artifact["path"])
                            target = safe_path(new_root, artifact["path"])
                            if not source.is_file():
                                raise MirrorError(f"Missing source file: {source}")
                            digest = sha256(source)
                            if target.exists() and sha256(target) != digest:
                                raise MirrorError(
                                    f"Destination already contains a different file: {target}"
                                )
                            if not target.exists():
                                durable_copy(source, target)
                            if sha256(target) != digest:
                                raise MirrorError(f"Copy verification failed: {target}")
                        for node in db.rows("nodes"):
                            if node["folder"]:
                                safe_path(new_root, node["path"]).mkdir(parents=True, exist_ok=True)
                        db.set_meta("migration_target", str(new_root))
                        self._save_settings(config)
                        db.set_meta("mirror_root", str(new_root))
                else:
                    self._save_settings(config)
            finally:
                db.close()
        self.config = config
        self.scheduler.interval = config.interval_seconds
        self.scheduler.poll = config.poll_seconds
        self.scheduler.next_sync = 0

    def _save_settings(self, config: Config):
        if config.launch_at_login != self.config.launch_at_login:
            set_launch_at_login(config.launch_at_login, self.data_dir)
        config.save(self.data_dir)

    def stop(self):
        self.cancel.set()
        self.scheduler.stop()
