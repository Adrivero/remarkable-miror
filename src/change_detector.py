from __future__ import annotations

from .config import Config
from .models import Document


def needs_download(
    doc: Document,
    artifact: dict | None,
    attempt: dict | None,
    config: Config,
    now: float,
    force: bool = False,
) -> bool:
    if force:
        return True
    if attempt:
        if attempt["status"] == "error":
            return True
        if attempt["token"] == doc.token and now - attempt["tried"] < config.refresh_days * 86400:
            return False
    return (
        artifact is None
        or artifact["token"] != doc.token
        or now - artifact["synced"] >= config.refresh_days * 86400
    )
