from __future__ import annotations

import threading
import time
from pathlib import Path
from uuid import UUID

import httpx

from .config import Config, device_url
from .events import Events
from .models import Cancelled, MirrorError, UnsupportedFormat


class USBClient:
    """Read-only HTTP transport. No redirects, environment proxies, or cloud API."""

    def __init__(
        self,
        config: Config,
        events: Events | None = None,
        transport: httpx.BaseTransport | None = None,
        cancel: threading.Event | None = None,
    ):
        self.config = config
        self.events = events or Events()
        self.cancel = cancel or threading.Event()
        self.http = httpx.Client(
            base_url=device_url(config.address),
            trust_env=False,
            headers={"Accept-Encoding": "identity"},
            follow_redirects=False,
            transport=transport,
            timeout=httpx.Timeout(config.read_timeout, connect=config.connect_timeout),
        )

    def close(self) -> None:
        self.http.close()

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Stopping safely; existing archives are preserved")

    def _retry(self, attempt: int, exc: Exception) -> None:
        self.check_cancelled()
        if attempt >= self.config.retries:
            raise MirrorError(f"USB request failed: {exc}") from exc
        self.events.emit("retry", f"USB request failed; retry {attempt + 1}: {exc}")
        if self.cancel.wait(min(2**attempt, 4)):
            self.check_cancelled()

    @staticmethod
    def _status(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code}", request=response.request, response=response
            )

    def list_folder(self, folder_id: str = "", *, probe: bool = False) -> list[dict]:
        if folder_id:
            folder_id = str(UUID(folder_id))
        for attempt in range(1 if probe else self.config.retries + 1):
            self.check_cancelled()
            try:
                response = self.http.get(
                    f"/documents/{folder_id}",
                    timeout=self.config.connect_timeout
                    if probe
                    else httpx.Timeout(
                        self.config.read_timeout, connect=self.config.connect_timeout
                    ),
                )
                self._status(response)
                raw = response.json()
                if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
                    raise MirrorError("USB API returned an unexpected document listing")
                return raw
            except (httpx.HTTPError, ValueError) as exc:
                if probe:
                    raise
                self._retry(attempt, exc)
        raise AssertionError("unreachable")

    def probe(self) -> tuple[str, str]:
        from .models import Document

        try:
            for row in self.list_folder(probe=True):
                Document.parse(row, "")
            return "connected", "USB API connected"
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            return (
                "disconnected",
                f"USB API unavailable. Check the cable and USB web interface setting. ({exc})",
            )
        except (httpx.HTTPError, MirrorError, ValueError) as exc:
            return "error", f"Unexpected USB API response: {exc}"

    def download(self, doc_id: str, fmt: str, target: Path, name: str) -> None:
        from .downloader import validate_artifact

        doc_id = str(UUID(doc_id))
        endpoints = ["pdf", "placeholder"] if fmt == "pdf" else ["rmdoc"]
        if fmt not in ("pdf", "rmdoc"):
            raise ValueError("Unsupported format")
        last_error: Exception | None = None
        unsupported = 0
        for endpoint in endpoints:
            for attempt in range(self.config.retries + 1):
                self.check_cancelled()
                self.events.emit(
                    "pdf" if fmt == "pdf" else "down",
                    f"{'Exporting' if fmt == 'pdf' else 'Downloading'} {name}.{fmt}",
                    name=name,
                    format=fmt,
                )
                try:
                    with self.http.stream("GET", f"/download/{doc_id}/{endpoint}") as response:
                        if response.status_code in (404, 405, 501):
                            unsupported += 1
                            break
                        self._status(response)
                        if "text/html" in response.headers.get("content-type", "").lower():
                            raise MirrorError("Tablet returned HTML instead of a document")
                        total = int(response.headers.get("content-length", "0")) or None
                        if total and total > self.config.max_download_bytes:
                            raise MirrorError("Export exceeds configured download size limit")
                        count, last_event = 0, 0.0
                        with target.open("wb") as out:
                            for chunk in response.iter_bytes(256 * 1024):
                                self.check_cancelled()
                                count += len(chunk)
                                if count > self.config.max_download_bytes:
                                    raise MirrorError(
                                        "Export exceeds configured download size limit"
                                    )
                                out.write(chunk)
                                if time.monotonic() - last_event > 0.1:
                                    self.events.emit(
                                        "bytes",
                                        f"Receiving {name}.{fmt}",
                                        received=count,
                                        total=total,
                                    )
                                    last_event = time.monotonic()
                            out.flush()
                            import os

                            os.fsync(out.fileno())
                        if total and count != total:
                            raise MirrorError("Incomplete download: length mismatch")
                    validate_artifact(target, fmt)
                    return
                except (httpx.HTTPError, OSError, MirrorError, ValueError) as exc:
                    if isinstance(exc, (Cancelled, OSError)):
                        raise
                    last_error = exc
                    if attempt < self.config.retries:
                        self._retry(attempt, exc)
                    else:
                        break
            # PDF endpoint fallback also handles firmware returning HTML/invalid PDF.
        if unsupported == len(endpoints):
            raise UnsupportedFormat(f"{name}: {fmt.upper()} export unavailable on this firmware")
        raise MirrorError(f"Could not export {name}.{fmt}: {last_error}") from last_error
