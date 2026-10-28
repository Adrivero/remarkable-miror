# reMarkable USB Mirror

Archive reMarkable documents over USB as PDF and RMDOC files. Run `remarkable-miror.py` to open the terminal interface. The app only reads from the tablet; it keeps local copies of documents removed from the device and preserves previous revisions and detected local edits.

## Setup

With `uv` installed, run this from the project folder:

```sh
uv run remarkable-miror.py
```

uv handles Python 3.12 or later and installs the dependencies declared inside the script automatically. No separate setup, activation, or package build is needed. The first run may need internet access to download Python and dependencies.

Connect the tablet with a USB data cable and enable its USB web interface. The default address is `10.11.99.1`. Press `p` to change settings.

Keep the terminal open for automatic syncing. The app syncs when the tablet connects, then every 15 minutes by default. It refreshes exports after seven days even if their metadata has not changed.

## Controls

| Key | Action |
| --- | --- |
| `s` | Sync now |
| `r` | Retry connection |
| `f` | Force full refresh |
| `p` | Settings |
| `l` | Saved logs |
| `o` | Open archive folder |
| `q` / `Ctrl+C` | Quit safely |

Settings include the archive folder, device address, intervals, export formats, and revision history. On macOS, launch at login opens a Terminal session. If you move this project, toggle that setting off and on to update the script path.

## Commands

```sh
uv run remarkable-miror.py status                 # Connectivity and archive statistics
uv run remarkable-miror.py scan                   # List tablet documents
uv run remarkable-miror.py sync --dry-run         # Preview downloads
uv run remarkable-miror.py sync                   # Sync once and exit
uv run remarkable-miror.py sync --force-refresh   # Download all selected formats again
uv run remarkable-miror.py verify                 # Check hashes and preserve local edits
uv run remarkable-miror.py --help
```

Commands print JSON results. `status` exits nonzero if disconnected, `sync` if incomplete, and `verify` if files are missing or changed. Verification can save copies of edited files.

## Files and settings

- **Documents:** `~/Documents/reMarkable Mirror/`
- **Settings and state:** `~/Library/Application Support/reMarkable USB Mirror/`
- **Inside the state folder:** `config.json`, `state.sqlite3`, `history/`, `conflicts/`, and `logs/` as needed.

Use Settings to change the archive folder or other options. Changing the archive folder through Settings copies files and keeps the originals. Back up both the archive and state folder.

To use a separate state folder, put `--data-dir` before the command:

```sh
uv run remarkable-miror.py --data-dir ./local-state status
```

This changes the settings and state location, not the document archive location.

## Troubleshooting

If disconnected, check the USB cable, the tablet's USB web interface setting, and the address in Settings. Export availability depends on the tablet's firmware; check logs if a format is unavailable. After an interrupted sync, run it again to recover pending work.

This archives documents exposed by the USB interface, not the tablet's entire system.

## Project structure

```text
remarkable-miror.py       Entry point and inline dependencies for uv
src/                     Application code
├── main.py              Command parsing and command execution
├── config.py            Defaults, settings validation, and config.json
├── usb_client.py        USB HTTP requests and document downloads
├── scanner.py           Walks the tablet's document hierarchy
├── change_detector.py   Decides which documents need downloading
├── downloader.py        Validates downloaded PDF and RMDOC files
├── sync_engine.py       Coordinates archiving, verification, and recovery
├── filesystem.py        Safe paths, file writes, locks, and preserved copies
├── database.py          SQLite tracking of documents and archive state
├── models.py            Shared document models, results, and errors
├── events.py            Progress events and persistent logs
├── scheduler.py         Connection polling and automatic sync timing
├── service.py           Connects background syncing with settings changes
├── login.py             macOS launch-at-login setup
└── tui/
    ├── app.py           Main terminal interface and keyboard actions
    └── screens.py       Settings, confirmation, and log screens
tests/                   Tests for syncing, USB requests, files, CLI, and UI
pytest.ini               Test configuration
README.md                Setup and usage instructions
```

`remarkable-miror.py` delegates to `main.py`, which opens the terminal interface or runs a single command. The interface uses `service.py` and `scheduler.py` for background work. Synchronization runs through `sync_engine.py`, using the USB client to read the tablet and the filesystem and database modules to maintain the local archive.

The `__init__.py` files mark Python module directories. Document exports, settings, and archive state live in the locations listed above, outside the source tree by default. uv manages the runtime environment; there is no build or packaging directory.

## Tests

Only needed when changing the code:

```sh
uv run --with pytest --with pytest-asyncio --with "httpx>=0.28,<1" --with "textual>=8.2,<9" --with "pypdf>=5,<7" python -m pytest
```

