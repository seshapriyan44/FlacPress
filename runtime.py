"""
Runtime helpers that let FlacPress behave the same whether it's running
from source or from a PyInstaller-built executable.

Three problems this solves:

  * Bundled files move. PyInstaller unpacks data files (static/, the
    bundled ffmpeg) into a temporary folder at launch and points
    sys._MEIPASS at it. Anything that looks for "static" relative to the
    source tree breaks inside a build, so paths have to be resolved
    through resource_path() instead.
  * ffmpeg usually isn't on PATH. A user who downloads a release has no
    reason to have installed ffmpeg, so find_tool() looks inside the
    build (and next to the .exe) before falling back to PATH.
  * Console windows flash. On Windows every subprocess spawns a console
    window unless told not to, which for a 200-file batch means 200
    black rectangles blinking over the UI. NO_WINDOW suppresses that and
    is passed to every subprocess call in core.py.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""


def is_frozen() -> bool:
    """True when running from a PyInstaller build."""
    return getattr(sys, "frozen", False)


def bundle_dir() -> Path:
    """Where bundled resources live (static/, bin/)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def app_dir() -> Path:
    """The folder the executable itself sits in.

    For a one-folder build this is where _internal/ lives; for a
    one-file build it's wherever the user put the .exe. Either way it's
    a sensible place for someone to drop their own ffmpeg.exe.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """Absolute path to a bundled resource, e.g. resource_path("static")."""
    return bundle_dir().joinpath(*parts)


def _usable(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.access(path, os.X_OK):
        return True
    try:  # data files extracted by PyInstaller can lose the +x bit
        path.chmod(path.stat().st_mode | 0o755)
        return os.access(path, os.X_OK)
    except OSError:
        return False


def find_tool(name: str) -> str:
    """Locate ffmpeg/ffprobe.

    Order: bundled with the build, next to the executable (bin/ or
    alongside it), then PATH. Falls back to the bare name so the
    "missing tool" message still comes from check_binaries() rather than
    a crash.
    """
    override = os.environ.get(f"FLACPRESS_{name.upper()}")
    filename = f"{name}{EXE_SUFFIX}"
    candidates = []
    if override:
        candidates.append(Path(override))
    candidates += [
        resource_path("bin", filename),
        app_dir() / "bin" / filename,
        app_dir() / filename,
    ]
    for candidate in candidates:
        if _usable(candidate):
            return str(candidate)
    return shutil.which(name) or name


def _no_window_kwargs() -> dict:
    """subprocess keyword arguments that keep Windows consoles hidden."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


NO_WINDOW = _no_window_kwargs()


def free_port(preferred: int = 5000, host: str = "127.0.0.1") -> int:
    """Return `preferred` if it's free, otherwise a nearby free port.

    Hardcoding 5000 is a common cause of "the app won't start" reports:
    it collides with other dev servers, and on macOS it's taken by
    AirPlay Receiver.
    """
    for candidate in [preferred, *range(preferred + 1, preferred + 20)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    """Block until the local server accepts connections (or time out).

    Without this the native window can open before Flask is listening
    and show a connection error instead of the UI.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.15)
    return False


def ffmpeg_source_note() -> Optional[str]:
    """Human-readable note about which ffmpeg is in use (for diagnostics)."""
    from core import FFMPEG  # imported lazily to avoid a circular import

    path = Path(FFMPEG)
    if not path.is_absolute():
        return None
    try:
        path.relative_to(bundle_dir())
        return "bundled with FlacPress"
    except ValueError:
        return str(path)
