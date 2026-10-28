from __future__ import annotations

import os
import plistlib
import shlex
import sys
from pathlib import Path

from .filesystem import fsync_dir, stage_path

LABEL = "local.remarkable-usb-mirror"


def set_launch_at_login(enabled: bool, data_dir: Path, *, agents_dir: Path | None = None) -> None:
    if sys.platform != "darwin":
        if enabled:
            raise RuntimeError("Launch at Login requires macOS")
        return
    directory = agents_dir or Path.home() / "Library/LaunchAgents"
    path = directory / f"{LABEL}.plist"
    # Removing the plist disables future logins without killing the running app.
    if not enabled:
        if path.exists():
            path.unlink()
        return
    executable = str(Path(sys.executable).absolute())
    command = [executable, str(Path(__file__).resolve().parent.parent / "remarkable-miror.py")]
    command += ["--data-dir", str(data_dir), "tui"]
    directory.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    # launchd has no TTY. A .command file opens a real Terminal session.
    launcher = data_dir / "launch-at-login.command"
    staged = stage_path(data_dir)
    try:
        with staged.open("w") as stream:
            stream.write("#!/bin/sh\nexec " + shlex.join(command) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(0o700)
        os.replace(staged, launcher)
        fsync_dir(data_dir)
    finally:
        staged.unlink(missing_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/open", "-a", "Terminal", str(launcher)],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardErrorPath": str(data_dir / "login-error.log"),
        "StandardOutPath": str(data_dir / "login-output.log"),
    }
    temp = stage_path(directory)
    try:
        with temp.open("wb") as stream:
            plistlib.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fsync_dir(directory)
    finally:
        temp.unlink(missing_ok=True)
    # launchd discovers user LaunchAgents at the next login. Do not bootstrap a
    # second copy while this app is already running.
