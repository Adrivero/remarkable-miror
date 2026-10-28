from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


class MirrorError(Exception):
    """An actionable sync failure; existing archives are preserved."""


class UnsupportedFormat(MirrorError):
    pass


class Cancelled(MirrorError):
    pass


@dataclass(frozen=True)
class Document:
    id: str
    parent: str
    name: str
    folder: bool
    metadata: dict[str, Any]

    @classmethod
    def parse(cls, raw: dict, parent: str) -> Document:
        try:
            ident = str(UUID(raw["ID"]))
            kind = raw["Type"]
            if kind not in ("CollectionType", "DocumentType"):
                raise ValueError(f"Unknown item type {kind!r}")
            name = raw.get("VisibleName", raw.get("VissibleName"))
            if not isinstance(name, str):
                raise ValueError("Missing document name")
            # Listing context is authoritative, but disagreement means an unstable scan.
            if "Parent" in raw and raw["Parent"] not in (parent, None):
                raise ValueError("Parent changed during scan; retry when tablet is idle")
            return cls(ident, parent, name, kind == "CollectionType", raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise MirrorError(f"Invalid USB document metadata: {exc}") from exc

    @property
    def token(self) -> str:
        # Names, parents, bookmarks and current page aren't content fingerprints.
        keys = (
            "contentHash",
            "contentVersion",
            "Version",
            "version",
            "ModifiedClient",
            "lastModified",
            "Size",
            "size",
            "fileType",
        )
        values = {k: self.metadata[k] for k in keys if self.metadata.get(k) is not None}
        return json.dumps(values, sort_keys=True, separators=(",", ":"))


@dataclass
class SyncResult:
    total: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    errors: int = 0
    unsupported: int = 0
    archived: int = 0
    complete: bool = False
    plan: list[dict] = field(default_factory=list)
