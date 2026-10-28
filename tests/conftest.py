import io
import json
import zipfile
from uuid import UUID

import httpx
import pytest
from pypdf import PdfWriter

from src.config import Config
from src.database import Database
from src.events import Events
from src.sync_engine import SyncEngine
from src.usb_client import USBClient


def uid(value):
    return str(UUID(int=value))


def doc(value=1, name="Notebook", parent="", folder=False, version="1", **extra):
    return {
        "ID": uid(value),
        "VisibleName": name,
        "Type": "CollectionType" if folder else "DocumentType",
        "Parent": parent,
        "ModifiedClient": version,
        **extra,
    }


def pdf(version="1"):
    writer = PdfWriter()
    writer.add_blank_page(width=100 + len(version), height=100)
    writer.add_metadata({"/Title": version})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def rmdoc(version="1"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("document.metadata", json.dumps({"version": version}))
        archive.writestr("document.content", "{}")
        archive.writestr("document/page.rm", version)
    return out.getvalue()


class Tablet:
    def __init__(self):
        self.folders = {"": [doc()]}
        self.requests = []
        self.downloads = []
        self.responses = {}
        self.fail_folder = None
        self.version = "1"

    def handle(self, request):
        self.requests.append(request)
        assert request.method == "GET"
        path = request.url.path
        if path.startswith("/documents/"):
            folder = path.removeprefix("/documents/")
            if folder == self.fail_folder:
                raise httpx.ConnectError("USB removed", request=request)
            return httpx.Response(200, json=self.folders[folder])
        self.downloads.append(path)
        override = self.responses.get(path)
        if callable(override):
            return override(request)
        if override is not None:
            return override
        fmt = path.rsplit("/", 1)[1]
        return httpx.Response(
            200, content=rmdoc(self.version) if fmt == "rmdoc" else pdf(self.version)
        )


class Harness:
    def __init__(self, tmp_path):
        self.config = Config(mirror_dir=str(tmp_path / "mirror"), retries=0)
        self.data = tmp_path / "data"
        self.root = tmp_path / "mirror"
        self.tablet = Tablet()
        self.received = []
        self.events = Events(self.received.append)
        self.client = USBClient(
            self.config, self.events, transport=httpx.MockTransport(self.tablet.handle)
        )

    def engine(self):
        return SyncEngine(self.config, self.data, self.events, self.client)

    def sync(self, **kwargs):
        return self.engine().run(**kwargs)

    def rows(self, table):
        db = Database(self.data)
        try:
            return db.rows(table)
        finally:
            db.close()

    def artifact_path(self, fmt="pdf", ident=1):
        row = next(
            a for a in self.rows("artifacts") if a["format"] == fmt and a["doc_id"] == uid(ident)
        )
        return self.root / row["path"]


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.client.close()
