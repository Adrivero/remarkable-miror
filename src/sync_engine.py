from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from uuid import uuid4

from .change_detector import needs_download
from .config import Config
from .database import Database
from .events import Events
from .filesystem import (
    PathAllocator,
    archive_lock,
    durable_copy,
    fsync_dir,
    name_key,
    preserve,
    safe_path,
    sha256,
    stage_path,
)
from .models import Cancelled, MirrorError, SyncResult, UnsupportedFormat
from .scanner import scan
from .usb_client import USBClient


class SyncEngine:
    def __init__(
        self,
        config: Config,
        data_dir: Path,
        events: Events | None = None,
        client: USBClient | None = None,
        cancel: threading.Event | None = None,
    ):
        config.validate()
        self.config, self.data_dir = config, data_dir.resolve()
        self.root = Path(config.mirror_dir).resolve()
        if self.root.is_relative_to(self.data_dir) or self.data_dir.is_relative_to(self.root):
            raise MirrorError("Mirror directory and application data must be separate")
        self.events = events or Events()
        self.cancel = cancel or threading.Event()
        self.client = client or USBClient(config, self.events, cancel=self.cancel)
        self.owns_client = client is None
        self.db: Database | None = None

    def _preserve(self, source: Path, ident: str, fmt: str, category: str, now: float) -> None:
        digest = sha256(source)
        path = preserve(source, self.data_dir, ident, fmt, category, digest)
        self.db.revision(ident, fmt, path, digest, now, category)
        self.events.emit(
            "warn" if category == "conflicts" else "info", f"Preserved {source.name} in {category}"
        )

    def _apply(self, journal_id: str, payload: dict) -> None:
        """Replayable install: durable intent -> replace -> DB -> source cleanup."""
        record = payload["artifact"]
        target = safe_path(self.root, record["path"])
        staged = safe_path(self.root, payload["staged"])
        old = payload.get("old")
        source = safe_path(self.root, old["path"]) if old else None
        now = record["synced"]
        if not target.exists() or sha256(target) != record["hash"]:
            if not staged.exists() or sha256(staged) != record["hash"]:
                raise MirrorError(
                    f"Recovery needs a valid staged file: {staged}. Existing archive retained."
                )
            if target.exists():
                expected = old["hash"] if old and source == target else None
                if sha256(target) != expected:
                    self._preserve(target, record["doc_id"], record["format"], "conflicts", now)
                elif self.config.history:
                    self._preserve(target, record["doc_id"], record["format"], "history", now)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
            fsync_dir(target.parent)
        self.db.commit_artifact(payload)
        if (
            source
            and source != target
            and source.exists()
            and not (name_key(str(source)) == name_key(str(target)) and source.samefile(target))
        ):
            source_hash = sha256(source)
            if source_hash != old["hash"]:
                self._preserve(source, record["doc_id"], record["format"], "conflicts", now)
            elif source_hash != record["hash"] and self.config.history:
                self._preserve(source, record["doc_id"], record["format"], "history", now)
            source.unlink()
            fsync_dir(source.parent)
            self._prune(source.parent)
        staged.unlink(missing_ok=True)
        self.db.journal_remove(journal_id)
        self.events.emit("ok", f"Saved and verified {record['path']}")

    def _prune(self, directory: Path) -> None:
        while directory != self.root and directory.is_relative_to(self.root):
            try:
                directory.rmdir()  # Never removes nonempty directories or retained documents.
            except OSError:
                break
            directory = directory.parent

    def _recover(self) -> None:
        pending = self.db.rows("journal")
        for row in pending:
            self.events.emit("info", "Recovering an interrupted file update")
            self._apply(row["id"], json.loads(row["payload"]))
        # Unjournaled temporary files are never authoritative. Leave them for explicit
        # diagnostics rather than recursively deleting files with a matching name.

    def _install(
        self,
        doc,
        fmt: str,
        relative: str,
        temp: Path,
        old: dict | None,
        now: float,
        token: str | None = None,
    ) -> bool:
        digest = sha256(temp)
        record = dict(
            doc_id=doc.id,
            format=fmt,
            path=relative,
            hash=digest,
            token=doc.token if token is None else token,
            synced=now,
        )
        target = safe_path(self.root, relative)
        if old and old["path"] == relative and target.exists() and sha256(target) == digest:
            # A forced export with identical bytes does not rewrite the active file.
            self.db.commit_artifact({"artifact": record})
            temp.unlink()
            return False
        journal_id = uuid4().hex
        payload = {
            "artifact": record,
            "staged": str(temp.relative_to(self.root)),
            "old": old,
            "refreshed": token is None,
        }
        self.db.journal_add(journal_id, payload)
        self._apply(journal_id, payload)
        return not old or digest != old["hash"]

    def _paths(self, docs, nodes, artifacts) -> dict[str, str]:
        allocator = PathAllocator(self.root, nodes, artifacts)
        paths = {"": ""}
        remaining = {d.id: d for d in docs}
        while remaining:
            ready = sorted((d for d in remaining.values() if d.parent in paths), key=lambda d: d.id)
            if not ready:
                raise MirrorError("Unresolvable remote folder hierarchy")
            for doc in ready:
                paths[doc.id] = allocator.allocate(paths[doc.parent], doc.name, doc.id, doc.folder)
                del remaining[doc.id]
        return paths

    def run(self, *, force: bool = False, dry_run: bool = False) -> SyncResult:
        result = SyncResult()
        try:
            with archive_lock(self.data_dir, self.root):
                self.db = Database(self.data_dir)
                bound = self.db.meta("mirror_root")
                if (
                    bound
                    and Path(bound) != self.root
                    and self.db.meta("migration_target") == str(self.root)
                ):
                    if not dry_run:
                        self.db.set_meta("mirror_root", str(self.root))
                    bound = str(self.root)
                if bound and Path(bound) != self.root:
                    raise MirrorError(
                        "This database belongs to another mirror directory. Restore that directory in Settings, or use a new data profile. See README for migration."
                    )
                if not dry_run:
                    self.db.set_meta("mirror_root", str(self.root))
                    self._recover()
                elif self.db.rows("journal"):
                    raise MirrorError("Pending recovery: run a normal sync before a dry run")
                docs = scan(self.client, self.events)
                nodes, artifacts = self.db.rows("nodes"), self.db.rows("artifacts")
                old_nodes = {row["id"]: row for row in nodes}
                artifact_formats: dict[str, list[str]] = {}
                for artifact in artifacts:
                    artifact_formats.setdefault(artifact["doc_id"], []).append(artifact["format"])
                paths = self._paths(docs, nodes, artifacts)
                now = time.time()
                result.total = sum(not d.folder for d in docs)
                if not dry_run:
                    result.missing = self.db.record_scan(docs, paths, now)
                    if result.missing:
                        self.events.emit(
                            "info", f"Retaining {result.missing} documents missing from tablet"
                        )
                    for doc in docs:
                        if doc.folder:
                            safe_path(self.root, paths[doc.id]).mkdir(parents=True, exist_ok=True)
                attempts = {(a["doc_id"], a["format"]): a for a in self.db.rows("attempts")}
                processed = 0
                for doc in docs:
                    if doc.folder:
                        continue
                    if self.cancel.is_set():
                        raise Cancelled("Synchronization stopped safely")
                    is_new = doc.id not in old_nodes
                    changed, failed, unavailable = False, False, False
                    if is_new:
                        self.events.emit("info", f"New document: {doc.name}")
                    # Reconcile disabled formats as well, preserving their old revision token.
                    formats = list(
                        dict.fromkeys(self.config.formats + artifact_formats.get(doc.id, []))
                    )
                    for fmt in formats:
                        old = self.db.artifact(doc.id, fmt)
                        relative = paths[doc.id] + "." + fmt
                        source = safe_path(self.root, old["path"]) if old else None
                        present = bool(source and source.is_file())
                        intact = bool(present and sha256(source) == old["hash"])
                        enabled = fmt in self.config.formats
                        attempt = attempts.get((doc.id, fmt))
                        download = enabled and (
                            needs_download(doc, old, attempt, self.config, now, force)
                            or (old is not None and not intact)
                        )
                        move = bool(old and old["path"] != relative and present)
                        if not enabled and not intact:
                            # A disabled format's local edits still survive, untouched.
                            move = False
                        action = "download" if download else "move" if move else "unchanged"
                        result.plan.append(
                            dict(id=doc.id, name=doc.name, format=fmt, action=action, path=relative)
                        )
                        if dry_run:
                            changed |= download or move
                            continue
                        if (
                            not download
                            and attempt
                            and attempt["status"] == "unsupported"
                            and enabled
                        ):
                            unavailable = True
                            result.unsupported += 1
                        if not download and not move:
                            continue
                        target = safe_path(self.root, relative)
                        temp = stage_path(target.parent)
                        try:
                            if download:
                                if old:
                                    self.events.emit(
                                        "info",
                                        f"Refreshing changed or due document: {doc.name}.{fmt}",
                                    )
                                self.client.download(doc.id, fmt, temp, doc.name)
                            else:
                                self.events.emit("info", f"Moving {old['path']} → {relative}")
                                durable_copy(source, temp)
                            changed |= self._install(
                                doc,
                                fmt,
                                relative,
                                temp,
                                old,
                                now if download else old["synced"],
                                token=None if download else old["token"],
                            )
                            changed |= move
                        except UnsupportedFormat as exc:
                            result.unsupported += 1
                            unavailable = True
                            self.db.attempt(doc, fmt, now, "unsupported", str(exc))
                            self.events.emit("warn", str(exc))
                        except Cancelled:
                            raise
                        except Exception as exc:
                            # A pending journal must be recovered before any further mutation.
                            if self.db.rows("journal"):
                                raise MirrorError(
                                    f"Interrupted persistence; recovery will run next sync: {exc}"
                                ) from exc
                            result.errors += 1
                            failed = True
                            self.db.attempt(doc, fmt, now, "error", str(exc))
                            self.events.emit("error", f"{doc.name}.{fmt}: {exc}")
                        finally:
                            if not self.db.rows("journal"):
                                temp.unlink(missing_ok=True)
                    if not dry_run:
                        self.db.finish_doc(
                            doc.id,
                            now,
                            "error" if failed else "unsupported" if unavailable else "synced",
                        )
                    if is_new:
                        result.new += 1
                    elif changed:
                        result.updated += 1
                    elif not failed:
                        result.unchanged += 1
                        if self.config.verbose:
                            self.events.emit("info", f"Unchanged: {doc.name}")
                    processed += 1
                    self.events.emit(
                        "progress",
                        f"Processed {doc.name}",
                        processed=processed,
                        total=result.total,
                        new=result.new,
                        updated=result.updated,
                        unchanged=result.unchanged,
                        errors=result.errors,
                    )
                if not dry_run:
                    for node in sorted(nodes, key=lambda n: len(n["path"]), reverse=True):
                        if node["folder"] and paths.get(node["id"]) != node["path"]:
                            self._prune(safe_path(self.root, node["path"]))
                    if result.errors == 0:
                        self.db.set_meta("last_success", str(time.time()))
                result.archived = len({a["doc_id"] for a in self.db.rows("artifacts")})
                result.complete = result.errors == 0
                self.events.emit(
                    "done",
                    f"{'Dry run' if dry_run else 'Sync'} complete: {result.new} new, {result.updated} updated, {result.unchanged} unchanged, {result.errors} errors, {result.unsupported} unavailable formats",
                    **{k: v for k, v in vars(result).items() if k != "plan"},
                )
                return result
        finally:
            if self.db:
                self.db.close()
            if self.owns_client:
                self.client.close()

    def verify(self) -> list[dict]:
        findings = []
        with archive_lock(self.data_dir, self.root):
            db = Database(self.data_dir)
            try:
                if db.meta("mirror_root") not in (None, str(self.root)):
                    raise MirrorError("Configured mirror differs from this database's archive")
                for row in db.rows("artifacts"):
                    path = safe_path(self.root, row["path"])
                    status = (
                        "missing"
                        if not path.exists()
                        else "ok"
                        if sha256(path) == row["hash"]
                        else "modified"
                    )
                    findings.append({"path": row["path"], "status": status})
                    if status == "modified":
                        saved = preserve(
                            path, self.data_dir, row["doc_id"], row["format"], "conflicts"
                        )
                        db.revision(
                            row["doc_id"],
                            row["format"],
                            saved,
                            sha256(path),
                            time.time(),
                            "conflicts",
                        )
                    self.events.emit("ok" if status == "ok" else "warn", f"{status}: {row['path']}")
            finally:
                db.close()
                if self.owns_client:
                    self.client.close()
        return findings
