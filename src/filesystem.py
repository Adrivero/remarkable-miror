from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .models import MirrorError


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = stage_path(path.parent)
    try:
        with temp.open("w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def stage_path(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".rm-stage-", dir=parent)
    os.close(fd)
    return Path(path)


def durable_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = stage_path(target.parent)
    try:
        with source.open("rb") as inp, temp.open("wb") as out:
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(source, temp)
        os.replace(temp, target)
        fsync_dir(target.parent)
    finally:
        temp.unlink(missing_ok=True)


def safe_path(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise MirrorError(f"Unsafe archive path: {relative}")
    path = root
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise MirrorError(f"Refusing symlink in archive: {path}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise MirrorError(f"Path escapes archive: {relative}")
    return path


def name_key(name: str) -> str:
    return unicodedata.normalize("NFD", name).casefold()


def sanitize(name: str) -> str:
    text = unicodedata.normalize("NFC", name)
    text = re.sub(r"[\x00-\x1f\x7f/\\:]", "_", text).strip(" .")
    if not text:
        text = "Untitled"
    while len(text.encode("utf-8")) > 150:
        text = text[:-1]
    return text


class PathAllocator:
    """Reserve missing items and existing user files; compare like macOS APFS."""

    def __init__(self, root: Path, nodes: list[dict], artifacts: list[dict]):
        self.root = root
        self.reserved: dict[str, str] = {}
        self.disk: dict[str, dict[str, Path]] = {}
        for node in nodes:
            if node["folder"]:
                self.reserved[name_key(node["path"])] = node["id"]
        for artifact in artifacts:
            self.reserved[name_key(artifact["path"])] = artifact["doc_id"]

    def allocate(self, parent: str, name: str, ident: str, folder: bool) -> str:
        base = sanitize(name)
        suffixes = ("",) if folder else (".pdf", ".rmdoc")
        # Include directory entries not managed by this application.
        directory = self.root / parent
        if parent not in self.disk:
            self.disk[parent] = (
                {name_key(p.name): p for p in directory.iterdir()} if directory.is_dir() else {}
            )
        disk = self.disk[parent]
        for index in range(10000):
            leaf = (
                base
                if index == 0
                else f"{base} [{ident[:8]}{'' if index == 1 else '-' + str(index)}]"
            )
            rel = str(Path(parent) / leaf)
            available = True
            for suffix in suffixes:
                key = name_key(rel + suffix)
                owner = self.reserved.get(key)
                on_disk = disk.get(name_key(leaf + suffix))
                if owner not in (None, ident) or (on_disk is not None and owner != ident):
                    available = False
            if available:
                for suffix in suffixes:
                    self.reserved[name_key(rel + suffix)] = ident
                return rel
        raise MirrorError(f"Could not allocate a unique filename for {name}")


@contextmanager
def archive_lock(data_dir: Path, mirror_dir: Path | None = None):
    """OS locks release automatically on crashes; also lock across data profiles."""
    handles = []
    try:
        for directory in dict.fromkeys([data_dir, *([mirror_dir] if mirror_dir else [])]):
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / ".mirror.lock"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(fd, "a+")
            handles.append(handle)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MirrorError("Another synchronization is already running") from exc
        yield
    finally:
        for handle in reversed(handles):
            handle.close()


def preserve(
    source: Path, data_dir: Path, ident: str, fmt: str, category: str, digest: str | None = None
) -> Path:
    digest = digest or sha256(source)
    # Deterministic history names deduplicate identical revisions. Conflicts are distinct.
    leaf = digest if category == "history" else f"{digest}-{uuid4().hex[:8]}"
    target = data_dir / category / ident / f"{leaf}.{fmt}"
    if not target.exists():
        durable_copy(source, target)
    return target


@contextmanager
def interface_lock(data_dir: Path):
    """One interactive monitor per profile; independent of the per-sync lock."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / ".tui.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MirrorError("A terminal interface is already running for this profile") from exc
        yield
