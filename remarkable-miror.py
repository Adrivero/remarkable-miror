#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28,<1", "textual>=8.2,<9", "pypdf>=5,<7"]
# ///
"""Run the reMarkable USB archive from this folder."""

from src.main import main


if __name__ == "__main__":
    raise SystemExit(main())
