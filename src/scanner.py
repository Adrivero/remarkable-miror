from __future__ import annotations

from .events import Events
from .models import Document, MirrorError
from .usb_client import USBClient


def scan(client: USBClient, events: Events) -> list[Document]:
    """Return only a complete tree. Any ambiguity aborts before missing transitions."""
    result: list[Document] = []
    seen: set[str] = set()
    pending = [("", 0)]
    while pending:
        parent, depth = pending.pop()
        if depth > 100:
            raise MirrorError("Folder nesting exceeds safety limit")
        events.emit("scan", f"Scanning {'root' if not parent else parent}")
        for raw in client.list_folder(parent):
            doc = Document.parse(raw, parent)
            if doc.id in seen:
                raise MirrorError("Duplicate UUID or folder cycle; remote scan is incomplete")
            seen.add(doc.id)
            result.append(doc)
            if len(result) > 1_000_000:
                raise MirrorError("Remote item count exceeds safety limit")
            if doc.folder:
                pending.append((doc.id, depth + 1))
    events.emit("info", f"Found {sum(not doc.folder for doc in result)} documents")
    return result
