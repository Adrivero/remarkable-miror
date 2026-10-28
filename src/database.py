from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Document, MirrorError

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, parent TEXT NOT NULL, name TEXT NOT NULL, folder INTEGER NOT NULL,
    metadata TEXT NOT NULL, path TEXT NOT NULL, last_seen REAL NOT NULL,
    last_sync REAL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    doc_id TEXT NOT NULL, format TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL,
    token TEXT NOT NULL, synced REAL NOT NULL, PRIMARY KEY(doc_id, format)
);
CREATE TABLE IF NOT EXISTS attempts (
    doc_id TEXT NOT NULL, format TEXT NOT NULL, token TEXT NOT NULL, tried REAL NOT NULL,
    status TEXT NOT NULL, message TEXT NOT NULL, PRIMARY KEY(doc_id, format)
);
CREATE TABLE IF NOT EXISTS journal (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions (
    doc_id TEXT NOT NULL, format TEXT NOT NULL, path TEXT PRIMARY KEY,
    hash TEXT NOT NULL, created REAL NOT NULL, kind TEXT NOT NULL
);
"""


class Database:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(data_dir / "state.sqlite3", timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise MirrorError(f"Unsupported database version {version}")
        self.connection.executescript(SCHEMA)
        self.connection.execute("PRAGMA user_version=1")

    def close(self) -> None:
        self.connection.close()

    def rows(self, table: str) -> list[dict]:
        if table not in ("nodes", "artifacts", "attempts", "journal", "revisions"):
            raise ValueError("Unknown table")
        return [dict(row) for row in self.connection.execute(f"SELECT * FROM {table}")]

    def meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def record_scan(self, docs: list[Document], paths: dict[str, str], now: float) -> int:
        # One transaction: a failed traversal never reaches this function.
        with self.connection:
            self.connection.execute("UPDATE nodes SET status='missing_from_device'")
            for doc in docs:
                self.connection.execute(
                    """INSERT INTO nodes
                    (id,parent,name,folder,metadata,path,last_seen,status) VALUES (?,?,?,?,?,?,?,'seen')
                    ON CONFLICT(id) DO UPDATE SET parent=excluded.parent,name=excluded.name,
                    folder=excluded.folder,metadata=excluded.metadata,path=excluded.path,
                    last_seen=excluded.last_seen,status='seen'""",
                    (
                        doc.id,
                        doc.parent,
                        doc.name,
                        doc.folder,
                        json.dumps(doc.metadata),
                        paths[doc.id],
                        now,
                    ),
                )
        return self.connection.execute(
            "SELECT count(*) FROM nodes WHERE folder=0 AND status='missing_from_device'"
        ).fetchone()[0]

    def artifact(self, ident: str, fmt: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE doc_id=? AND format=?", (ident, fmt)
        ).fetchone()
        return dict(row) if row else None

    def commit_artifact(self, payload: dict) -> None:
        record = payload["artifact"]
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?)",
                tuple(record[k] for k in ("doc_id", "format", "path", "hash", "token", "synced")),
            )
            if payload.get("refreshed", True):
                self.connection.execute(
                    "DELETE FROM attempts WHERE doc_id=? AND format=?",
                    (record["doc_id"], record["format"]),
                )
            # Keep the journal until source cleanup is durable as well.

    def journal_add(self, ident: str, payload: dict) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO journal VALUES (?,?)", (ident, json.dumps(payload))
            )

    def journal_remove(self, ident: str) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM journal WHERE id=?", (ident,))

    def revision(
        self, ident: str, fmt: str, path: Path, digest: str, now: float, kind: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO revisions VALUES (?,?,?,?,?,?)",
                (ident, fmt, str(path), digest, now, kind),
            )

    def attempt(self, doc: Document, fmt: str, now: float, status: str, message: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO attempts VALUES (?,?,?,?,?,?)",
                (doc.id, fmt, doc.token, now, status, message),
            )

    def finish_doc(self, ident: str, now: float, status: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE nodes SET status=?, last_sync=CASE WHEN ?='synced' THEN ? ELSE last_sync END WHERE id=?",
                (status, status, now, ident),
            )
