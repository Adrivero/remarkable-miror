from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DEFAULT_DATA, Config
from .database import Database
from .events import Events, log_setup
from .filesystem import archive_lock, interface_lock
from .models import MirrorError
from .scanner import scan
from .sync_engine import SyncEngine
from .usb_client import USBClient


def parser():
    result = argparse.ArgumentParser(description="Read-only reMarkable USB archiving")
    result.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    sub = result.add_subparsers(dest="command")
    sub.add_parser("tui", help="Open the interactive terminal interface (default)")
    sub.add_parser("status", help="Show device connectivity and archive statistics")
    sub.add_parser("scan", help="List the entire accessible remote hierarchy")
    sync = sub.add_parser("sync", help="Synchronize the local archive")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--force-refresh", action="store_true")
    sub.add_parser("verify", help="Check local hashes and preserve detected local edits")
    return result


def run_tui(config: Config, data_dir: Path) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise MirrorError(
            "The TUI requires an interactive terminal. Use status, scan, sync, or verify for scripts."
        )
    from .tui.app import MirrorApp

    with interface_lock(data_dir):
        app = MirrorApp(config, data_dir)
        try:
            return app.run() or 0
        finally:
            app.service.stop()
            thread = app.service.scheduler.thread
            if thread:
                thread.join(config.read_timeout + 10)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    try:
        config = Config.load(data_dir)
        if args.command in (None, "tui"):
            return run_tui(config, data_dir)
        events = Events(
            lambda event: (
                print(event.line(), file=sys.stderr)
                if event.kind not in ("bytes", "progress", "schedule")
                else None
            ),
            log_setup(data_dir),
        )
        if args.command == "sync":
            result = SyncEngine(config, data_dir, events).run(
                force=args.force_refresh, dry_run=args.dry_run
            )
            print(json.dumps(vars(result), indent=2, ensure_ascii=False))
            return 0 if result.complete else 1
        if args.command == "verify":
            findings = SyncEngine(config, data_dir, events).verify()
            print(json.dumps(findings, indent=2))
            return int(any(f["status"] != "ok" for f in findings))
        client = USBClient(config, events)
        try:
            if args.command == "scan":
                with archive_lock(data_dir, Path(config.mirror_dir)):
                    docs = scan(client, events)
                print(json.dumps([d.metadata for d in docs], indent=2, ensure_ascii=False))
                return 0
            state, detail = client.probe()
            db = Database(data_dir)
            try:
                result = dict(
                    device=state,
                    detail=detail,
                    mirror=config.mirror_dir,
                    archived=len({a["doc_id"] for a in db.rows("artifacts")}),
                    missing=sum(
                        n["status"] == "missing_from_device" and not n["folder"]
                        for n in db.rows("nodes")
                    ),
                    last_success=db.meta("last_success"),
                    pending_recovery=len(db.rows("journal")),
                )
            finally:
                db.close()
            print(json.dumps(result, indent=2))
            return 0 if state == "connected" else 1
        finally:
            client.close()
    except KeyboardInterrupt:
        print("Interrupted; recovery will run at next synchronization", file=sys.stderr)
        return 130
    except (MirrorError, OSError, ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
