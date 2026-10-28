import json
from dataclasses import replace

import pytest

from src import main


def test_cli_status(h, monkeypatch, capsys):
    h.config.save(h.data)
    monkeypatch.setattr(main, "USBClient", lambda *args: h.client)
    assert main.main(["--data-dir", str(h.data), "status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["device"] == "connected" and result["archived"] == 0


def test_cli_scan(h, monkeypatch, capsys):
    h.config.save(h.data)
    monkeypatch.setattr(main, "USBClient", lambda *args: h.client)
    assert main.main(["--data-dir", str(h.data), "scan"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


@pytest.mark.parametrize(
    "args", [["sync"], ["sync", "--dry-run"], ["sync", "--force-refresh"], ["verify"]]
)
def test_cli_engine_commands(h, monkeypatch, capsys, args):
    h.config.save(h.data)
    monkeypatch.setattr(main, "SyncEngine", lambda *args: h.engine())
    assert main.main(["--data-dir", str(h.data), *args]) == 0
    assert json.loads(capsys.readouterr().out) is not None


def test_cli_invalid_configuration_reports_actionable_error(h, capsys):
    h.data.mkdir()
    (h.data / "config.json").write_text('{"address":"example.com"}')
    assert main.main(["--data-dir", str(h.data), "sync"]) == 1
    assert "Error:" in capsys.readouterr().err


def test_migration_recovers_config_saved_before_binding_commit(h, monkeypatch):
    from src.config import Config
    from src.database import Database
    from src.service import Service

    h.sync()
    h.config.save(h.data)
    new = replace(h.config, mirror_dir=str(h.root.parent / "relocated"))
    original = Database.set_meta

    def crash(db, key, value):
        if key == "mirror_root":
            raise OSError("simulated exit before root binding commit")
        return original(db, key, value)

    monkeypatch.setattr(Database, "set_meta", crash)
    with pytest.raises(OSError):
        Service(h.config, h.data, h.events).apply_config(new)
    h.config = Config.load(h.data)
    assert h.config.mirror_dir == new.mirror_dir
    monkeypatch.setattr(Database, "set_meta", original)
    h.tablet.downloads.clear()
    assert h.sync().complete
    assert not h.tablet.downloads
