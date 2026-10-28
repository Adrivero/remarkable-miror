import json
from pathlib import Path

import httpx
import pytest
from conftest import doc, pdf, uid

from src.database import Database
from src.filesystem import archive_lock, sha256
from src.models import MirrorError


def test_initial_and_unchanged_no_downloads_or_timestamp_changes(h):
    result = h.sync()
    assert result.complete and result.new == 1 and result.archived == 1
    assert len(h.tablet.downloads) == 2
    before = {p: p.stat().st_mtime_ns for p in h.root.iterdir() if not p.name.startswith(".")}
    h.tablet.downloads.clear()
    assert h.sync().unchanged == 1
    assert not h.tablet.downloads
    assert before == {p: p.stat().st_mtime_ns for p in before}


def test_new_notebook(h):
    h.sync()
    h.tablet.folders[""].append(doc(2, "New"))
    h.tablet.downloads.clear()
    result = h.sync()
    assert result.new == 1 and result.unchanged == 1
    assert len(h.tablet.downloads) == 2


def test_modified_notebook_preserves_history(h):
    h.sync()
    old = h.artifact_path().read_bytes()
    h.tablet.version = "two"
    h.tablet.folders[""][0]["ModifiedClient"] = "two"
    result = h.sync()
    assert result.updated == 1
    assert h.artifact_path().read_bytes() != old
    assert any(Path(r["path"]).read_bytes() == old for r in h.rows("revisions"))


@pytest.mark.parametrize("name", ["Renamed", "NOTEBOOK", "é笔记../bad:name"])
def test_rename_without_download(h, name):
    h.sync()
    h.tablet.downloads.clear()
    h.tablet.folders[""][0]["VisibleName"] = name
    assert h.sync().updated == 1
    assert not h.tablet.downloads
    assert h.artifact_path().exists()
    assert len(list(h.root.glob("*.pdf"))) == 1


def test_nested_folder_move_and_rename(h):
    h.tablet.folders = {
        "": [doc(10, "Research", folder=True)],
        uid(10): [doc(11, "Math", uid(10), True)],
        uid(11): [doc(parent=uid(11))],
    }
    h.sync()
    assert h.artifact_path() == h.root / "Research/Math/Notebook.pdf"
    h.tablet.downloads.clear()
    h.tablet.folders[""][0]["VisibleName"] = "Study"
    h.sync()
    assert h.artifact_path() == h.root / "Study/Math/Notebook.pdf"
    assert not (h.root / "Research").exists()
    h.tablet.folders[uid(11)] = []
    h.tablet.folders[""].append(doc())
    h.sync()
    assert h.artifact_path() == h.root / "Notebook.pdf"
    assert not h.tablet.downloads


def test_folder_move(h):
    h.tablet.folders = {
        "": [doc(10, "A", folder=True), doc(11, "B", folder=True)],
        uid(10): [doc(parent=uid(10))],
        uid(11): [],
    }
    h.sync()
    h.tablet.downloads.clear()
    h.tablet.folders[""] = [doc(11, "B", folder=True)]
    h.tablet.folders[uid(11)] = [doc(10, "A", uid(11), True)]
    h.sync()
    assert h.artifact_path() == h.root / "B/A/Notebook.pdf"
    assert not h.tablet.downloads


def test_duplicate_names_case_and_unicode_collision(h):
    h.tablet.folders[""] = [doc(1, "Same"), doc(2, "same"), doc(3, "é"), doc(4, "e\u0301")]
    h.sync()
    files = [h.artifact_path(ident=i) for i in range(1, 5)]
    assert len(set(files)) == 4
    assert all(p.exists() for p in files)
    h.tablet.downloads.clear()
    h.sync()
    assert not h.tablet.downloads


def test_missing_retained_and_returning_same_identity(h):
    h.sync()
    path = h.artifact_path()
    before = path.stat().st_mtime_ns
    h.tablet.folders[""] = []
    assert h.sync().missing == 1
    assert path.exists()
    assert h.rows("nodes")[0]["status"] == "missing_from_device"
    h.tablet.downloads.clear()
    h.tablet.folders[""] = [doc()]
    assert h.sync().missing == 0
    assert not h.tablet.downloads and path.stat().st_mtime_ns == before


def test_missing_name_reserved_for_new_document(h):
    h.sync()
    path = h.artifact_path()
    h.tablet.folders[""] = [doc(2)]
    h.sync()
    assert h.artifact_path(ident=2) != path
    assert path.exists()


def test_folder_rename_retains_missing_descendant_in_last_location(h):
    h.tablet.folders = {
        "": [doc(10, "A", folder=True)],
        uid(10): [doc(parent=uid(10)), doc(2, "Kept", uid(10))],
    }
    h.sync()
    retained = h.artifact_path(ident=2)
    h.tablet.folders[uid(10)].pop()
    h.tablet.folders[""][0]["VisibleName"] = "B"
    h.sync()
    assert retained.exists()
    assert h.artifact_path() == h.root / "B/Notebook.pdf"


def test_incomplete_scan_never_marks_missing(h):
    h.sync()
    before = h.rows("nodes")
    h.tablet.folders[""] = [doc(10, "Folder", folder=True)]
    h.tablet.fail_folder = uid(10)
    with pytest.raises(MirrorError):
        h.sync()
    assert h.rows("nodes") == before
    assert h.artifact_path().exists()


def test_duplicate_uuid_aborts_scan(h):
    h.sync()
    h.tablet.folders[""].append(doc())
    with pytest.raises(MirrorError, match="Duplicate"):
        h.sync()
    assert h.rows("nodes")[0]["status"] == "synced"


def test_download_disconnect_preserves_old_and_other_format_succeeds(h):
    h.sync()
    before = h.artifact_path().read_bytes()
    h.tablet.version = "next"
    h.tablet.folders[""][0]["ModifiedClient"] = "next"

    def disconnected(request):
        raise httpx.ReadError("USB disconnected", request=request)

    for endpoint in ("pdf", "placeholder"):
        h.tablet.responses[f"/download/{uid(1)}/{endpoint}"] = disconnected
    result = h.sync()
    assert result.errors == 1 and not result.complete
    assert h.artifact_path().read_bytes() == before
    assert h.rows("attempts")[0]["format"] == "pdf"
    h.tablet.responses.clear()
    h.tablet.downloads.clear()
    assert h.sync().complete
    assert len(h.tablet.downloads) == 1


@pytest.mark.parametrize("bad", [b"<html>Error</html>", b"%PDF-1.7\npartial", b"%PDF-1.7\n%%EOF"])
def test_corrupt_pdf_rejected(h, bad):
    h.sync()
    before = h.artifact_path().read_bytes()
    for endpoint in ("pdf", "placeholder"):
        h.tablet.responses[f"/download/{uid(1)}/{endpoint}"] = httpx.Response(200, content=bad)
    assert h.sync(force=True).errors == 1
    assert h.artifact_path().read_bytes() == before


def test_pdf_fallback_and_unsupported_native(h):
    h.tablet.responses[f"/download/{uid(1)}/pdf"] = httpx.Response(404)
    h.tablet.responses[f"/download/{uid(1)}/rmdoc"] = httpx.Response(501)
    assert h.sync().unsupported == 1
    assert h.artifact_path().exists()
    h.tablet.downloads.clear()
    h.sync()
    assert not h.tablet.downloads
    h.sync(force=True)
    assert h.tablet.downloads


def test_local_edits_preserved_and_restored(h):
    h.sync()
    h.artifact_path().write_bytes(b"my edited local PDF")
    assert h.sync().complete
    assert h.artifact_path().read_bytes() == pdf()
    assert any(
        Path(r["path"]).read_bytes() == b"my edited local PDF"
        for r in h.rows("revisions")
        if r["kind"] == "conflicts"
    )


def test_verify_preserves_local_edits_without_network(h):
    h.sync()
    h.artifact_path().write_bytes(b"local edits")
    h.tablet.requests.clear()
    assert any(f["status"] == "modified" for f in h.engine().verify())
    assert h.artifact_path().read_bytes() == b"local edits"
    assert not h.tablet.requests
    assert list((h.data / "conflicts").rglob("*.pdf"))


def test_force_refresh_identical_preserves_timestamp_and_history(h):
    h.sync()
    path = h.artifact_path()
    before = path.stat().st_mtime_ns
    h.tablet.downloads.clear()
    h.sync(force=True)
    assert len(h.tablet.downloads) == 2
    assert path.stat().st_mtime_ns == before
    assert not h.rows("revisions")


def test_periodic_refresh_without_metadata(h):
    del h.tablet.folders[""][0]["ModifiedClient"]
    h.sync()
    h.tablet.downloads.clear()
    h.sync()
    assert not h.tablet.downloads
    db = Database(h.data)
    with db.connection:
        db.connection.execute("UPDATE artifacts SET synced=0")
    db.close()
    h.sync()
    assert len(h.tablet.downloads) == 2


def test_crash_after_file_replace_before_database_commit(h, monkeypatch):
    h.sync()
    h.tablet.version = "updated"
    h.tablet.folders[""][0]["ModifiedClient"] = "updated"
    real_commit = Database.commit_artifact

    def crash(*args):
        raise RuntimeError("simulated process termination")

    monkeypatch.setattr(Database, "commit_artifact", crash)
    with pytest.raises(MirrorError, match="Interrupted persistence"):
        h.sync()
    assert h.rows("journal")
    monkeypatch.setattr(Database, "commit_artifact", real_commit)
    assert h.sync().complete
    assert not h.rows("journal")
    for artifact in h.rows("artifacts"):
        assert sha256(h.root / artifact["path"]) == artifact["hash"]


def test_crash_before_file_replace_replays_staged_file(h, monkeypatch):
    h.sync()
    h.tablet.version = "new"
    h.tablet.folders[""][0]["ModifiedClient"] = "new"
    engine = h.engine()
    monkeypatch.setattr(
        engine, "_apply", lambda *args: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(MirrorError):
        engine.run()
    row = h.rows("journal")[0]
    assert (h.root / json.loads(row["payload"])["staged"]).exists()
    h.sync()
    assert not h.rows("journal")


def test_lock_prevents_overlapping_cli_or_tui(h):
    with archive_lock(h.data, h.root):
        with pytest.raises(MirrorError, match="already running"):
            h.sync()
    assert not h.tablet.requests


def test_dry_run_does_not_download_or_change_document_state(h):
    h.sync()
    before = h.rows("nodes")
    h.tablet.folders[""].append(doc(2))
    h.tablet.downloads.clear()
    result = h.sync(dry_run=True)
    assert result.new == 1
    assert h.rows("nodes") == before and not h.tablet.downloads


def test_restart_uses_database(h):
    h.sync()
    h.tablet.downloads.clear()
    assert h.engine().run().unchanged == 1
    assert not h.tablet.downloads


def test_live_events_and_only_get_requests(h):
    h.sync()
    kinds = [e.kind for e in h.received]
    assert all(k in kinds for k in ("scan", "down", "pdf", "bytes", "ok", "progress", "done"))
    assert any("Notebook.pdf" in e.message for e in h.received if e.kind == "ok")
    assert all(r.method == "GET" for r in h.tablet.requests)


def test_symlink_archive_file_refused(h, tmp_path):
    h.sync()
    path = h.artifact_path()
    outside = tmp_path / "outside"
    outside.write_text("precious")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(MirrorError, match="symlink"):
        h.sync()
    assert outside.read_text() == "precious"


def test_unmanaged_target_is_not_overwritten(h):
    h.root.mkdir()
    (h.root / "Notebook.pdf").write_text("personal file")
    h.sync()
    assert (h.root / "Notebook.pdf").read_text() == "personal file"
    assert h.artifact_path().name != "Notebook.pdf"


def test_partial_stream_disconnect_preserves_previous_files(h):
    h.sync()
    originals = {row["path"]: (h.root / row["path"]).read_bytes() for row in h.rows("artifacts")}

    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"%PDF-1.7\npartial"
            raise httpx.ReadError("USB removed halfway through response")

    for endpoint in ("pdf", "placeholder"):
        h.tablet.responses[f"/download/{uid(1)}/{endpoint}"] = lambda request: httpx.Response(
            200, stream=BrokenStream()
        )
    assert h.sync(force=True).errors == 1
    assert all((h.root / path).read_bytes() == content for path, content in originals.items())
    assert not list(h.root.rglob(".rm-stage-*"))


def test_crash_after_commit_before_move_cleanup(h, monkeypatch):
    h.sync()
    old_path = h.artifact_path()
    h.tablet.folders[""][0]["VisibleName"] = "Moved"
    real_commit = Database.commit_artifact

    def commit_then_crash(db, payload):
        real_commit(db, payload)
        raise RuntimeError("process ended after SQLite commit")

    monkeypatch.setattr(Database, "commit_artifact", commit_then_crash)
    with pytest.raises(MirrorError):
        h.sync()
    assert h.rows("journal") and old_path.exists()
    monkeypatch.setattr(Database, "commit_artifact", real_commit)
    h.sync()
    assert not old_path.exists() and h.artifact_path().exists()
    assert not h.rows("journal")


def test_recovery_preserves_edit_made_to_old_source(h, monkeypatch):
    h.sync()
    h.tablet.folders[""][0]["VisibleName"] = "Renamed"
    engine = h.engine()
    monkeypatch.setattr(
        engine, "_apply", lambda *args: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(MirrorError):
        engine.run()
    h.artifact_path("rmdoc").write_bytes(b"local modification after crash")
    h.sync()
    assert any(
        Path(r["path"]).read_bytes() == b"local modification after crash"
        for r in h.rows("revisions")
    )


def test_moving_stale_unsupported_format_does_not_advance_revision(h):
    h.sync()
    h.tablet.folders[""][0]["ModifiedClient"] = "2"
    h.tablet.responses[f"/download/{uid(1)}/rmdoc"] = httpx.Response(501)
    h.sync()
    h.tablet.folders[""][0]["VisibleName"] = "Moved"
    assert h.sync().unsupported == 1
    assert h.rows("attempts")[0]["status"] == "unsupported"
    h.tablet.downloads.clear()
    assert h.sync().unsupported == 1
    assert not h.tablet.downloads


def test_enabled_formats_after_preferences_change(h):
    h.config.formats = ["pdf"]
    h.sync()
    assert len(h.rows("artifacts")) == 1
    h.config.formats = ["pdf", "rmdoc"]
    h.tablet.downloads.clear()
    h.sync()
    assert h.tablet.downloads == [f"/download/{uid(1)}/rmdoc"]


def test_remote_revision_not_advanced_by_failed_export(h):
    h.sync()
    original = next(a for a in h.rows("artifacts") if a["format"] == "pdf")
    h.tablet.folders[""][0]["ModifiedClient"] = "2"
    for endpoint in ("pdf", "placeholder"):
        h.tablet.responses[f"/download/{uid(1)}/{endpoint}"] = httpx.Response(500)
    h.sync()
    current = next(a for a in h.rows("artifacts") if a["format"] == "pdf")
    assert current == original
