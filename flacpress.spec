# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller build recipe for FlacPress.

Build locally on Windows:
    pip install -r requirements.txt
    pyinstaller flacpress.spec            # -> dist/FlacPress.exe

Two shapes are supported, chosen with an environment variable:

    FLACPRESS_ONEFILE=1   (default)  single self-contained FlacPress.exe
    FLACPRESS_ONEFILE=0              a folder containing FlacPress.exe

One-file is the nicer download, but it has to unpack everything to a temp
folder on every launch — with ffmpeg bundled that's a noticeable pause on
slow disks. The one-folder build starts instantly. CI produces both.

ffmpeg: if bin/ffmpeg(.exe) and bin/ffprobe(.exe) exist they get bundled,
and runtime.find_tool() will prefer them over anything on PATH. If they
aren't there the build still works, it just relies on the user having
ffmpeg installed.
"""

import os
import sys
from pathlib import Path

<<<<<<< HEAD
from PyInstaller.utils.hooks import collect_submodules

=======
>>>>>>> aa375041aabf5387bdb65f01e89cb55aa1f0db9f
ROOT = Path(SPECPATH).resolve()
ONEFILE = os.environ.get("FLACPRESS_ONEFILE", "1") != "0"
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""

# ---------------------------------------------------------------- resources

datas = [(str(ROOT / "static"), "static")]

# Bundled as data rather than binaries on purpose: these are statically
# linked, self-contained executables, so there's nothing for PyInstaller's
# dependency scanner to find and pointing it at ~90 MB files just makes the
# build slower.
bundled_tools = []
for tool in ("ffmpeg", "ffprobe"):
    candidate = ROOT / "bin" / f"{tool}{EXE_SUFFIX}"
    if candidate.is_file():
        datas.append((str(candidate), "bin"))
        bundled_tools.append(candidate.name)

print(f"[flacpress.spec] mode={'one-file' if ONEFILE else 'one-folder'}")
print(f"[flacpress.spec] bundled tools: {bundled_tools or 'none (will use PATH)'}")

# ------------------------------------------------------------ hidden imports

# pywebview picks its GUI backend at runtime, so PyInstaller's static
# analysis can't see which platform module is needed.
hiddenimports = ["webview"]
if sys.platform == "win32":
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium",
                      "clr"]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["webview.platforms.gtk", "webview.platforms.qt"]

<<<<<<< HEAD
# tagging.py's mutagen.File(path) auto-detects the container by trying every
# format mutagen supports, not just the ones this project's own code imports
# by name (FLAC/MP3/MP4/AIFF/WAVE/OggOpus/OggVorbis). mutagen's own __init__
# already imports that full list itself, so PyInstaller's static analysis
# should trace it regardless — but that reasoning is exactly the kind of
# thing that's cheap to get wrong and expensive to debug from a bug report
# with no traceback, so the whole package is collected explicitly rather
# than trusted to transitive analysis.
hiddenimports += collect_submodules("mutagen")

# Likewise for this project's own modules: they're plain top-level imports
# (app.py imports providers/enrich/tagging/runtime unconditionally), which
# PyInstaller's analysis starting from desktop.py should already follow —
# but the fix-library feature (enrich/providers/tagging) has no equivalent
# to the CI step below that actually exercises it inside the built exe, so
# there's currently no automated way to notice if that tracing ever silently
# stops working. Listed explicitly until such a check exists.
hiddenimports += ["core", "app", "enrich", "providers", "tagging", "runtime"]

=======
>>>>>>> aa375041aabf5387bdb65f01e89cb55aa1f0db9f
icon_path = ROOT / "static" / "assets" / "icon.ico"

a = Analysis(
    ["desktop.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="FlacPress",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(icon_path) if icon_path.is_file() else None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="FlacPress",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(icon_path) if icon_path.is_file() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="FlacPress",
    )
