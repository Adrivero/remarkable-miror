from __future__ import annotations

import ipaddress
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

APP_NAME = "reMarkable USB Mirror"
DEFAULT_DATA = Path.home() / "Library/Application Support" / APP_NAME


def device_url(address: str) -> str:
    """Only literal private/link-local IPs; no DNS, credentials, proxies or redirects."""
    url = urlsplit(address if "://" in address else "http://" + address)
    if url.scheme != "http" or url.username or url.password or url.query or url.fragment:
        raise ValueError("Use an HTTP USB address without credentials, query or fragment")
    if url.path not in ("", "/"):
        raise ValueError("Device address must not contain a path")
    ip = ipaddress.ip_address(url.hostname or "")
    if not (ip.is_private or ip.is_link_local) or ip.is_unspecified or ip.is_multicast:
        raise ValueError("Use the tablet's private USB IP address")
    port = url.port  # validates port
    host = f"[{ip}]" if ip.version == 6 else str(ip)
    return f"http://{host}" + (f":{port}" if port else "")


@dataclass
class Config:
    mirror_dir: str = str(Path.home() / "Documents/reMarkable Mirror")
    address: str = "10.11.99.1"
    interval_seconds: int = 900
    poll_seconds: int = 10
    refresh_days: float = 7
    formats: list[str] = field(default_factory=lambda: ["rmdoc", "pdf"])
    history: bool = True
    verbose: bool = False
    launch_at_login: bool = False
    connect_timeout: float = 3
    read_timeout: float = 120
    retries: int = 2
    max_download_bytes: int = 4 * 1024**3

    def validate(self) -> None:
        device_url(self.address)
        if not isinstance(self.mirror_dir, str) or not self.mirror_dir.strip():
            raise ValueError("Choose an archive directory")
        if (
            self.interval_seconds < 30
            or self.poll_seconds < 2
            or not math.isfinite(self.refresh_days)
            or self.refresh_days <= 0
        ):
            raise ValueError("Interval must be >=30s, polling >=2s, refresh period >0 days")
        if not self.formats or set(self.formats) - {"pdf", "rmdoc"}:
            raise ValueError("Select PDF, RMDOC, or both")
        if self.connect_timeout <= 0 or self.read_timeout <= 0 or not 0 <= self.retries <= 5:
            raise ValueError("Invalid timeout or retry setting")
        if self.max_download_bytes <= 0:
            raise ValueError("Maximum download size must be positive")
        self.mirror_dir = str(Path(self.mirror_dir).expanduser().absolute())

    @classmethod
    def load(cls, data_dir: Path) -> Config:
        path = data_dir / "config.json"
        config = cls(**json.loads(path.read_text())) if path.exists() else cls()
        config.validate()
        return config

    def save(self, data_dir: Path) -> None:
        from .filesystem import atomic_json

        self.validate()
        atomic_json(data_dir / "config.json", asdict(self))
