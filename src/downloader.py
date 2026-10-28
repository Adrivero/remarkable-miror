from __future__ import annotations

import json
import zipfile
from pathlib import Path, PurePosixPath

from pypdf import PdfReader

from .models import MirrorError


def validate_artifact(path: Path, fmt: str) -> None:
    try:
        if fmt == "pdf":
            with path.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise ValueError("Missing PDF header")
                stream.seek(max(0, path.stat().st_size - 1024))
                if b"%%EOF" not in stream.read():
                    raise ValueError("Missing PDF end marker")
                reader = PdfReader(path, strict=True)
                if reader.is_encrypted or not len(reader.pages):
                    raise ValueError("Encrypted or empty PDF export")
                for page in reader.pages:
                    _ = page.mediabox
        elif fmt == "rmdoc":
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if not entries or len(entries) > 100_000:
                    raise ValueError("Empty or oversized archive index")
                if sum(item.file_size for item in entries) > 8 * 1024**3:
                    raise ValueError("Uncompressed archive exceeds 8 GiB safety limit")
                for item in entries:
                    p = PurePosixPath(item.filename)
                    if p.is_absolute() or ".." in p.parts or "\\" in item.filename:
                        raise ValueError("Unsafe archive member path")
                if archive.testzip():
                    raise ValueError("Archive CRC mismatch")
                for suffix in (".content", ".metadata"):
                    members = [item for item in entries if item.filename.endswith(suffix)]
                    if not members:
                        raise ValueError(f"Archive has no {suffix} metadata")
                    for member in members:
                        if member.file_size > 16 * 1024**2:
                            raise ValueError("Oversized archive metadata")
                        if not isinstance(json.loads(archive.read(member)), dict):
                            raise ValueError("Invalid archive metadata")
        else:
            raise ValueError("Unknown artifact format")
    except Exception as exc:
        raise MirrorError(f"Invalid {fmt.upper()} export: {exc}") from exc
