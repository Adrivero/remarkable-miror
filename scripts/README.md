# Demo GIF

From the repository root, run:

```sh
uv run scripts/render_demo.py
```

The renderer captures the actual Textual interface at 100 × 32 terminal cells and writes `docs/demo.gif`. It uses simulated connection and export events with sample notebook names, then opens Settings through the app's keyboard binding. This is an interface demonstration, not a recording of a live tablet transfer.

The background service stays disabled. Temporary state is removed after capture; the renderer never connects to a tablet or uses your real archive or settings.

CairoSVG requires the Cairo system library. On macOS with Homebrew, install `cairo` and, if necessary, run with its library directory:

```sh
DYLD_FALLBACK_LIBRARY_PATH="$(brew --prefix cairo)/lib" uv run scripts/render_demo.py
```
