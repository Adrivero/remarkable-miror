import plistlib
import zipfile
from pathlib import Path

import httpx
import pytest
from conftest import doc

from src.config import Config, device_url
from src.downloader import validate_artifact
from src.events import Events, log_setup
from src.filesystem import safe_path, sanitize
from src.login import set_launch_at_login
from src.models import Document, MirrorError
from src.usb_client import USBClient


@pytest.mark.parametrize(
    "address",
    [
        "https://10.11.99.1",
        "http://user:password@10.11.99.1",
        "8.8.8.8",
        "example.com",
        "http://10.11.99.1/upload",
        "http://10.11.99.1?x=1",
    ],
)
def test_usb_url_rejects_unwanted_destinations(address):
    with pytest.raises(ValueError):
        device_url(address)


def test_usb_url_and_spelling():
    assert device_url("10.11.99.1") == "http://10.11.99.1"
    raw = doc()
    raw["VissibleName"] = raw.pop("VisibleName")
    assert Document.parse(raw, "").name == "Notebook"


def test_probe_disconnected_and_unexpected_response(h):
    def fail(request):
        raise httpx.ConnectError("USB unavailable", request=request)

    client = USBClient(h.config, transport=httpx.MockTransport(fail))
    assert client.probe()[0] == "disconnected"
    client.close()
    client = USBClient(
        h.config, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    )
    assert client.probe()[0] == "error"
    client.close()


def test_redirects_not_followed(h):
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://example.com"})

    client = USBClient(h.config, transport=httpx.MockTransport(redirect))
    assert client.probe()[0] == "error"
    assert len(calls) == 1
    client.close()


def test_retry_is_bounded_and_logged(h, monkeypatch):
    h.config.retries = 2
    h.tablet.fail_folder = ""
    monkeypatch.setattr(h.client.cancel, "wait", lambda delay: False)
    with pytest.raises(MirrorError):
        h.sync()
    assert len(h.tablet.requests) == 3
    assert len([e for e in h.received if e.kind == "retry"]) == 2


def test_invalid_uuid_and_path_traversal(tmp_path):
    raw = doc()
    raw["ID"] = "../../outside"
    with pytest.raises(MirrorError):
        Document.parse(raw, "")
    with pytest.raises(MirrorError):
        safe_path(tmp_path, "../outside")
    assert "/" not in sanitize("../../unsafe/name")
    assert len(sanitize("笔" * 100).encode()) <= 150


def test_malformed_zip_rejected(tmp_path):
    path = tmp_path / "bad.rmdoc"
    path.write_bytes(b"PKbad")
    with pytest.raises(MirrorError):
        validate_artifact(path, "rmdoc")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../bad.content", "{}")
        archive.writestr("document.metadata", "{}")
    with pytest.raises(MirrorError, match="Unsafe"):
        validate_artifact(path, "rmdoc")


def test_download_length_and_size_limits(h):
    h.config.max_download_bytes = 10
    assert h.sync().errors == 2
    assert not h.rows("artifacts")


def test_disk_write_failure_keeps_previous_copy(h, monkeypatch):
    h.sync()
    original = h.artifact_path().read_bytes()

    def disk_full(*args):
        raise OSError("No space left on device")

    monkeypatch.setattr(h.client, "download", disk_full)
    assert h.sync(force=True).errors == 2
    assert h.artifact_path().read_bytes() == original


def test_history_disabled_still_preserves_conflicts(h):
    h.config.history = False
    h.sync()
    h.artifact_path().write_bytes(b"local changes")
    h.sync()
    assert any(r["kind"] == "conflicts" for r in h.rows("revisions"))
    assert not any(r["kind"] == "history" for r in h.rows("revisions"))


def test_persistent_logs_and_config(tmp_path):
    events = Events(logger=log_setup(tmp_path))
    events.emit("ok", "Saved Notebook.pdf")
    assert "Saved Notebook.pdf" in (tmp_path / "logs/mirror.log").read_text()
    config = Config(mirror_dir=str(tmp_path / "mirror"))
    config.save(tmp_path)
    assert Config.load(tmp_path) == config


def test_login_item_enable_disable(tmp_path, monkeypatch):
    import src.login as login

    monkeypatch.setattr(login.sys, "platform", "darwin")
    directory = tmp_path / "agents"
    set_launch_at_login(True, tmp_path / "data", agents_dir=directory)
    path = next(directory.glob("*.plist"))
    payload = plistlib.loads(path.read_bytes())
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"][:3] == ["/usr/bin/open", "-a", "Terminal"]
    assert not payload.get("KeepAlive")
    import shlex

    launcher = Path(payload["ProgramArguments"][3])
    command = shlex.split(launcher.read_text().splitlines()[1].removeprefix("exec "))
    assert command[0] == str(Path(login.sys.executable).absolute())
    assert command[1] == str(Path(login.__file__).resolve().parent.parent / "remarkable-usb-mirror.py")
    assert command[-1] == "tui"
    assert launcher.stat().st_mode & 0o100
    set_launch_at_login(False, tmp_path / "data", agents_dir=directory)
    assert not path.exists()
