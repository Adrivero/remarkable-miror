# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28,<1", "textual>=8.2,<9", "pypdf>=5,<7", "CairoSVG>=2.7,<3", "Pillow>=11,<13"]
# ///
"""Render the real TUI with simulated events, isolated from user data and USB."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import cairosvg
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import Config
from src.events import Event
from src.tui.app import MirrorApp, Snapshot
from textual.widgets._header import HeaderClock


async def render() -> None:
    frames, durations = [], []
    fixed_time = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    with TemporaryDirectory(prefix="remarkable-demo-") as temporary:
        config = Config(mirror_dir=str(Path(temporary) / "archive"))
        os.environ.pop("NO_COLOR", None)
        app = MirrorApp(config, Path(temporary) / "state", start_service=False)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.query_one(HeaderClock).display = False
            # Display a portable example path without touching the user's archive.
            app.config.mirror_dir = "~/Documents/reMarkable Mirror"
            app.archive.update(app.config.mirror_dir)

            async def capture(milliseconds: int = 800) -> None:
                await pilot.pause(0.08)
                svg = app.export_screenshot(title="remarkable-usb-mirror · sample demo")
                svg = svg.replace("font-family: Fira Code, monospace;", "font-family: Menlo, DejaVu Sans Mono, monospace;")
                png = cairosvg.svg2png(bytestring=svg.encode(), output_width=1200)
                frames.append(Image.open(BytesIO(png)).convert("RGB"))
                durations.append(milliseconds)

            def event(kind: str, message: str, **data) -> None:
                app.handle_event(Event(kind, message, data, at=fixed_time))

            event("connection", "Waiting for the USB web interface", status="disconnected")
            await capture(1600)
            event("connection", "USB web interface ready", status="connected")
            await capture(1200)
            await pilot.press("s")
            event("busy", "Starting sample sync", active=True)
            event("scan", "Scanning tablet documents…")
            await capture(900)
            for index, name in enumerate(["Meeting notes", "Project sketches", "Reading journal"]):
                event("progress", "Document progress", processed=index, total=3,
                      new=index, updated=0, unchanged=0, errors=0)
                for fmt in ["pdf", "rmdoc"]:
                    event("pdf" if fmt == "pdf" else "down", f"Exporting {name}.{fmt}")
                    for received in [0, 2, 5, 8]:
                        event("bytes", "Receiving", received=received * 1024**2, total=8 * 1024**2)
                        await capture(240)
                    event("info", f"Saved {name}.{fmt}")
            event("progress", "Document progress", processed=3, total=3,
                  new=3, updated=0, unchanged=0, errors=0)
            await capture(700)
            event("done", "Sample sync complete: 3 documents, 0 errors", total=3, errors=0)
            event("busy", "Ready", active=False)
            await pilot.pause()
            app.on_snapshot(Snapshot({"archived": 3, "missing": 0, "last_success": None}))
            app.schedule.update("Last sync: Just now   ·   Next: In 15 minutes")
            await capture(2400)
            await pilot.press("p")
            await capture(3200)
            await pilot.press("escape")
            await capture(1400)
    output = ROOT / "docs/demo.gif"
    output.parent.mkdir(exist_ok=True)
    frames[0].save(output, save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=True, disposal=2)
    print(f"Saved {output} ({len(frames)} frames, {sum(durations)/1000:.1f}s)")


if __name__ == "__main__":
    asyncio.run(render())
