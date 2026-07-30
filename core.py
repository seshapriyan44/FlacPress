"""
FlacPress core conversion engine.

This module is UI-agnostic: both the Flask web app (app.py) and the CLI
(cli.py) drive it through ConversionJob, which reports progress via a
plain callback so it can be wired to Server-Sent Events, a print
statement, a Qt signal, or whatever else.

Notable behaviors:
  * Output goes to a destination you choose, mirroring the source's folder
    structure. Only the leaf folder that directly contains audio files
    (the album folder) gets the format name appended to it — everything
    above that in the path is preserved unchanged. See resolve_output_path().
  * All metadata is copied via mutagen, not ffmpeg's -map_metadata. That
    turned out to be unreliable in two independent ways, both confirmed
    against a real ffmpeg build: some ffmpeg Ogg muxers write *no*
    metadata at all for Opus/Vorbis output, and even where it works,
    multi-valued fields (e.g. two contributing artists) get collapsed into
    a single semicolon-joined string instead of preserved as distinct
    values the way a real tagger would. mutagen's Easy* interfaces handle
    both correctly.
  * Cover-art handling is format-aware: Ogg containers have no stream-copy
    equivalent for embedded art, so it's written as a METADATA_BLOCK_PICTURE
    tag via mutagen after encoding instead. MP3/AAC still use ffmpeg's
    native stream copy, with an automatic retry-without-art fallback if
    that ever fails for some other reason (corrupt/odd source image).
  * .m4a is ambiguous (lossless ALAC vs. lossy AAC) — the real codec is
    probed with ffprobe and already-lossy files are skipped by default.
  * Bitrate/quality choices are presented as named presets per format
    rather than freeform text.
  * Conversion runs via Popen (not run()) so cancel() can actually kill
    in-flight ffmpeg processes instead of waiting for the whole batch.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from runtime import NO_WINDOW, find_tool

# Tags, cover art and synced lyrics live in tagging.py, which knows how each
# container stores them. That module exists because ffmpeg cannot be trusted
# with this: some of its Ogg muxers write no metadata at all for Opus/Vorbis
# output, and even where the mapping does work, multi-valued fields (two
# contributing artists, say) get flattened into one semicolon-joined string
# instead of staying distinct values. enrich.py builds on the same layer to
# fill in whatever a file is missing.
import enrich
import providers
import tagging

MUTAGEN_AVAILABLE = tagging.MUTAGEN_AVAILABLE

# ==========================
# Format + bitrate presets
# ==========================

LOSSLESS_EXTENSIONS = {
    ".flac", ".wav", ".wave", ".w64", ".aiff", ".aif", ".aifc", ".ape",
    ".alac", ".m4a", ".wv", ".tta", ".tak", ".shn", ".caf", ".dsf", ".dff",
    ".mlp",
}

# Output formats that don't throw anything away. Kept as a set rather than a
# flag on each preset so the three original lossy presets stay untouched.
LOSSLESS_FORMATS = {"flac", "alac", "wav", "aiff"}


def is_lossless_output(fmt: str) -> bool:
    return fmt in LOSSLESS_FORMATS

FORMAT_PRESETS = {
    "opus": {
        "ext": ".opus",
        "base_args": ["-c:a", "libopus", "-vbr", "on"],
        # Ogg has no stream-copy equivalent for cover art (unlike MP4/ID3
        # containers) — art goes in as a METADATA_BLOCK_PICTURE tag after
        # encoding instead. tagging.py does that for every format now.
        "embed_cover": False,
        "ogg_container": True,
        "bitrate_options": [
            {"id": "96k", "flag": "-b:a", "value": "96k", "label": "96 kbps",
             "desc": "Good — small files, solid quality for casual listening"},
            {"id": "128k", "flag": "-b:a", "value": "128k", "label": "128 kbps",
             "desc": "Very good — great everyday balance of size and quality"},
            {"id": "160k", "flag": "-b:a", "value": "160k", "label": "160 kbps",
             "desc": "Excellent — transparent for most listeners", "default": True},
            {"id": "192k", "flag": "-b:a", "value": "192k", "label": "192 kbps",
             "desc": "Archival — indistinguishable from source for critical listening"},
        ],
    },
    "mp3": {
        "ext": ".mp3",
        "base_args": ["-c:a", "libmp3lame"],
        "embed_cover": True,
        "bitrate_options": [
            {"id": "v4", "flag": "-q:a", "value": "4", "label": "V4 (~165 kbps VBR)",
             "desc": "Good — noticeably smaller, fine for casual or mobile listening"},
            {"id": "v2", "flag": "-q:a", "value": "2", "label": "V2 (~190 kbps VBR)",
             "desc": "Very good — strong everyday quality/size balance"},
            {"id": "v0", "flag": "-q:a", "value": "0", "label": "V0 (~245 kbps VBR)",
             "desc": "Excellent — best VBR quality, near-transparent", "default": True},
            {"id": "320k", "flag": "-b:a", "value": "320k", "label": "320 kbps CBR",
             "desc": "Archival — max compatibility with older hardware/car stereos"},
        ],
    },
    "aac": {
        "ext": ".m4a",
        "base_args": ["-c:a", "aac"],
        "embed_cover": True,
        "bitrate_options": [
            {"id": "128k", "flag": "-b:a", "value": "128k", "label": "128 kbps",
             "desc": "Good — compact size, solid for casual listening"},
            {"id": "192k", "flag": "-b:a", "value": "192k", "label": "192 kbps",
             "desc": "Excellent — recommended default, great balance", "default": True},
            {"id": "256k", "flag": "-b:a", "value": "256k", "label": "256 kbps",
             "desc": "Archival — near-transparent, larger files"},
        ],
    },
    # ---------------------------------------------------------------- lossless
    # These re-package audio without discarding anything, for when the point
    # is compatibility rather than saving space: ALAC for Apple devices, FLAC
    # for everything else, WAV/AIFF for editing software and old hardware.
    # "Quality" here means compression effort or bit depth, never loss, hence
    # the per-option "args" override instead of a bitrate flag.
    "flac": {
        "ext": ".flac",
        "base_args": ["-c:a", "flac"],
        "embed_cover": False,
        "bitrate_options": [
            {"id": "c5", "args": ["-compression_level", "5"],
             "label": "Level 5 (default)",
             "desc": "Lossless — quick to encode, sensible file size",
             "default": True},
            {"id": "c8", "args": ["-compression_level", "8"],
             "label": "Level 8 (smaller)",
             "desc": "Lossless — noticeably smaller, slower to encode"},
            {"id": "c12", "args": ["-compression_level", "12"],
             "label": "Level 12 (smallest)",
             "desc": "Lossless — smallest FLAC possible, slowest to encode"},
        ],
    },
    "alac": {
        "ext": ".m4a",
        "base_args": ["-c:a", "alac"],
        "embed_cover": False,
        "bitrate_options": [
            {"id": "std", "args": [], "label": "Lossless",
             "desc": "Apple Lossless — for iPhone, iTunes and Apple Music",
             "default": True},
        ],
    },
    "wav": {
        "ext": ".wav",
        "base_args": [],
        "embed_cover": False,
        "bitrate_options": [
            {"id": "s16", "args": ["-c:a", "pcm_s16le"], "label": "16-bit",
             "desc": "Uncompressed CD quality — universally readable",
             "default": True},
            {"id": "s24", "args": ["-c:a", "pcm_s24le"], "label": "24-bit",
             "desc": "Uncompressed studio depth — very large files"},
        ],
    },
    "aiff": {
        "ext": ".aiff",
        "base_args": [],
        "embed_cover": False,
        "bitrate_options": [
            {"id": "s16", "args": ["-c:a", "pcm_s16be"], "label": "16-bit",
             "desc": "Uncompressed CD quality — the Mac equivalent of WAV",
             "default": True},
            {"id": "s24", "args": ["-c:a", "pcm_s24be"], "label": "24-bit",
             "desc": "Uncompressed studio depth — very large files"},
        ],
    },
}

# Resolved through find_tool() rather than shutil.which() alone: a user who
# downloads a release has no reason to have ffmpeg on PATH, so the copy
# shipped inside the build (or dropped next to the .exe) has to win first.
FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def _bitrate_option(fmt: str, bitrate_id: Optional[str]) -> dict:
    options = FORMAT_PRESETS[fmt]["bitrate_options"]
    if bitrate_id:
        for opt in options:
            if opt["id"] == bitrate_id:
                return opt
    return next((o for o in options if o.get("default")), options[0])


def build_audio_args(fmt: str, bitrate_id: Optional[str]) -> tuple[list[str], str]:
    preset = FORMAT_PRESETS[fmt]
    opt = _bitrate_option(fmt, bitrate_id)
    # Lossy presets say flag/value ("-b:a 320k"); lossless ones give a
    # complete "args" list instead, because what varies is the encoder or the
    # compression level rather than a bitrate.
    if "args" in opt:
        extra = [str(part) for part in opt["args"]]
    else:
        extra = [opt["flag"], str(opt["value"])]
    return [*preset["base_args"], *extra], preset["ext"]


def check_binaries() -> list[str]:
    """Returns a list of missing required binaries (empty = all good)."""
    missing = []
    for name, path in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        try:
            subprocess.run(
                [path, "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
                **NO_WINDOW,
            )
        except Exception:
            missing.append(name)
    return missing


def probe_audio_codec(file: Path) -> Optional[str]:
    """Best-effort: returns the codec name of the first audio stream, or None."""
    try:
        result = subprocess.run(
            [
                FFPROBE, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name", "-of", "json", str(file),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, **NO_WINDOW,
        )
        data = json.loads(result.stdout or b"{}")
        streams = data.get("streams") or []
        if streams:
            return streams[0].get("codec_name")
    except Exception:
        return None
    return None


def verify_output(file: Path) -> bool:
    if not file.exists() or file.stat().st_size == 0:
        return False
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", str(file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, **NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        return False


def scan_files(source_dir: Path, exclude_root: Optional[Path] = None, extensions=LOSSLESS_EXTENSIONS) -> list[Path]:
    """Walk source_dir for lossless files. exclude_root (typically the
    destination folder) is only treated as an exclusion boundary if it
    actually lives inside source_dir — if it's a sibling, a parent (the
    common case now that destination defaults to source_dir.parent), or
    somewhere unrelated, there's nothing to exclude, and treating it as
    one anyway would incorrectly wipe out the entire scan whenever
    exclude_root happens to be an ancestor of source_dir."""
    source_dir = source_dir.resolve()
    active_exclude = None
    if exclude_root:
        candidate = exclude_root.resolve()
        if candidate == source_dir or source_dir in candidate.parents:
            active_exclude = candidate

    files = []
    for path in source_dir.rglob("*"):
        if not path.is_file():
            continue
        if active_exclude and (path == active_exclude or active_exclude in path.parents):
            continue
        if path.suffix.lower() in extensions:
            files.append(path)
    return files


def resolve_output_path(src: Path, source_dir: Path, destination_root: Path, suffix: str, out_ext: str) -> Path:
    """Mirrors the source folder structure under destination_root, but only
    renames the leaf folder that directly contains the audio file — the
    "album folder" — by appending the format suffix (e.g. "OPUS") to its
    name. Everything above that in the path is preserved as-is.

    Examples (suffix="OPUS"):
      source_dir = E:\\Songs\\Some Album              (files directly inside)
        -> destination_root\\Some Album OPUS\\track.opus

      source_dir = E:\\Songs\\English\\The Weeknd
      file in    E:\\Songs\\English\\The Weeknd\\2011 - House Of Balloons\\
        -> destination_root\\2011 - House Of Balloons OPUS\\track.opus

      source_dir = E:\\Songs   (broad scan covering many artists/albums)
      file in    E:\\Songs\\English\\The Weeknd\\2011 - House Of Balloons\\
        -> destination_root\\English\\The Weeknd\\2011 - House Of Balloons OPUS\\track.opus
    """
    rel_dir = src.parent.relative_to(source_dir)
    if rel_dir == Path("."):
        # the file sits directly in source_dir — source_dir itself is the "album folder"
        target_dir = destination_root / f"{source_dir.name} {suffix}"
    else:
        parts = rel_dir.parts
        leaf = f"{parts[-1]} {suffix}"
        target_dir = destination_root.joinpath(*parts[:-1], leaf)
    return (target_dir / src.name).with_suffix(out_ext)


# Metadata, cover art and synced lyrics for the converted file are written by
# tagging.copy_all(), which covers every container FlacPress can produce:
# Vorbis comments plus picture blocks for FLAC and Ogg, ID3 frames for MP3,
# WAV and AIFF, and MP4 atoms for AAC and ALAC. This file used to carry its
# own per-format tag copier and a separate Ogg cover embedder; both moved into
# tagging.py so the library fixer in enrich.py could share them.


@dataclass
class JobConfig:
    source_dir: Path
    destination_dir: Optional[Path] = None  # None => source_dir.parent (sibling of source)
    # NOTE: paths are normalised in __post_init__ — see the comment there.
    # Everything downstream assumes source_dir is absolute and resolved.
    output_format: str = "opus"
    bitrate: Optional[str] = None   # bitrate preset id, e.g. "160k" or "v0" — see FORMAT_PRESETS
    workers: int = field(default_factory=lambda: max(1, (os.cpu_count() or 4) - 2))
    dry_run: bool = False
    force: bool = False           # re-convert even if destination already exists
    verify: bool = True           # ffprobe-validate every output file
    skip_lossy_m4a: bool = True   # skip .m4a sources that are already lossy AAC
    embed_cover_art: bool = True  # carry the source's cover art into the output

    # Optionally fill in what the *source* never had, on the converted copy.
    # Off by default: conversion should be predictable, and looking things up
    # online is a separate decision from re-encoding. The standalone library
    # fixer (enrich.EnrichJob) is the same machinery without the conversion.
    fix_missing_metadata: bool = False
    fix_missing_art: bool = False
    fix_missing_lyrics: bool = False
    online_lookups: bool = True   # applies to the three flags above

    def __post_init__(self):
        """Normalise the paths once, here, so nothing downstream has to.

        scan_files() resolves source_dir before walking it, so every path it
        returns is absolute. _convert_one() then calls
        src.relative_to(config.source_dir) — which raises ValueError for
        every single file if config.source_dir is still relative, because an
        absolute path is never "inside" a relative one. Same for a path
        containing "~" or a symlink.

        The practical symptom was that `cli.py some/relative/folder` (or a
        relative path typed into the web UI) failed on every file while the
        exact same folder given as an absolute path worked fine.
        """
        self.source_dir = Path(self.source_dir).expanduser().resolve()
        if self.destination_dir is not None:
            self.destination_dir = Path(self.destination_dir).expanduser().resolve()


def _build_command(src: Path, dst: Path, audio_args: list[str]) -> list[str]:
    """ffmpeg handles audio only; tagging.py handles everything else.

    Cover art used to be stream-copied here with "-map 0 -c:v copy", which
    meant a source image the target container disliked failed the whole
    encode and needed a retry without it. Mapping only the audio removes
    that failure mode entirely, and mutagen re-attaches the artwork
    afterwards — which it has to do for FLAC, Ogg, WAV and AIFF regardless.
    """
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src),
        *audio_args,
        "-map", "0:a",
        "-map_metadata", "0",
        str(dst),
    ]


class ConversionJob:
    """One batch run. Call .run() on a background thread; call .cancel()
    from any thread to stop it (kills in-flight ffmpeg processes)."""

    def __init__(self, config: JobConfig, on_event: Callable[[dict], None]):
        self.config = config
        self.on_event = on_event
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._active_procs: dict[int, subprocess.Popen] = {}
        self._slots: "queue.Queue[int]" = queue.Queue()
        for i in range(config.workers):
            self._slots.put(i)
        self.stats = {"converted": 0, "skipped": 0, "skipped_lossy": 0, "failed": 0, "total": 0}

    def cancel(self):
        self._cancel.set()
        with self._lock:
            for proc in self._active_procs.values():
                try:
                    proc.terminate()
                except Exception:
                    pass

    def emit(self, event_type: str, **data):
        self.on_event({"type": event_type, "ts": time.time(), **data})

    def _run_ffmpeg(self, slot: int, command: list[str]) -> tuple[int, bytes]:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **NO_WINDOW)
        with self._lock:
            self._active_procs[slot] = proc
        try:
            _, stderr = proc.communicate()
        finally:
            with self._lock:
                self._active_procs.pop(slot, None)
        return proc.returncode, stderr

    def _convert_one(self, src: Path, destination_root: Path, audio_args: list[str],
                      out_ext: str, suffix: str) -> str:
        if self._cancel.is_set():
            return "cancelled"

        slot = self._slots.get()
        relative = src.relative_to(self.config.source_dir)
        try:
            dst = resolve_output_path(src, self.config.source_dir, destination_root, suffix, out_ext)
            dst.parent.mkdir(parents=True, exist_ok=True)
            self.emit("file_start", slot=slot, file=str(relative))

            if dst.exists() and not self.config.force:
                self.emit("file_done", slot=slot, file=str(relative), status="skipped")
                return "skipped"

            if self.config.skip_lossy_m4a and src.suffix.lower() == ".m4a":
                codec = probe_audio_codec(src)
                if codec and codec.lower() != "alac":
                    self.emit("file_done", slot=slot, file=str(relative),
                               status="skipped_lossy", detail=f"already {codec}")
                    return "skipped_lossy"

            if self.config.dry_run:
                self.emit("file_done", slot=slot, file=str(relative), status="dry")
                return "dry"

            command = _build_command(src, dst, audio_args)
            returncode, stderr = self._run_ffmpeg(slot, command)

            if self._cancel.is_set():
                dst.unlink(missing_ok=True)
                return "cancelled"

            if returncode != 0:
                dst.unlink(missing_ok=True)
                self.emit("file_done", slot=slot, file=str(relative), status="failed",
                           detail=stderr.decode(errors="ignore")[-400:].strip())
                return "failed"

            if self.config.verify and not verify_output(dst):
                dst.unlink(missing_ok=True)
                self.emit("file_done", slot=slot, file=str(relative), status="failed", detail="verify failed")
                return "failed"

            notes = []

            # Tags, cover art and lyrics, via mutagen rather than ffmpeg.
            copied = tagging.copy_all(src, dst,
                                      include_art=self.config.embed_cover_art,
                                      include_lyrics=True)
            if not MUTAGEN_AVAILABLE:
                notes.append("tags skipped (mutagen not installed)")
            elif copied.get("error"):
                notes.append(f"tags: {copied['error']}")
            elif copied.get("art"):
                notes.append("cover art embedded")

            # Optionally fill gaps the source itself never had.
            if (self.config.fix_missing_metadata or self.config.fix_missing_art
                    or self.config.fix_missing_lyrics):
                filled = enrich.enrich_file(
                    dst,
                    enrich.EnrichConfig(
                        source_dir=dst.parent,
                        fix_metadata=self.config.fix_missing_metadata,
                        fix_art=self.config.fix_missing_art,
                        fix_lyrics=self.config.fix_missing_lyrics,
                        use_online=self.config.online_lookups,
                        dry_run=False,
                    ),
                    # Guess from the *source* path: the output folder has the
                    # format name appended ("Discovery OPUS"), which would
                    # otherwise be read back as the album title.
                    name_source=src,
                )
                if filled.get("detail"):
                    notes.append(f"filled in {filled['detail']}")

            self.emit("file_done", slot=slot, file=str(relative), status="converted",
                       detail=" · ".join(notes) or None)
            return "converted"
        except Exception as exc:
            self.emit("file_done", slot=slot, file=str(relative), status="failed", detail=str(exc))
            return "failed"
        finally:
            self._slots.put(slot)

    def run(self):
        cfg = self.config
        missing = check_binaries()
        if missing:
            self.emit("error", detail=f"Missing required tool(s): {', '.join(missing)}. "
                                       f"Install ffmpeg and put it on PATH, or place "
                                       f"ffmpeg and ffprobe next to FlacPress.")
            return

        audio_args, out_ext = build_audio_args(cfg.output_format, cfg.bitrate)
        suffix = cfg.output_format.upper()

        if cfg.online_lookups and (cfg.fix_missing_metadata or cfg.fix_missing_art
                                   or cfg.fix_missing_lyrics):
            providers.network.reset()

        source_dir = cfg.source_dir.resolve()
        destination_root = (cfg.destination_dir or source_dir.parent).resolve()

        files = scan_files(source_dir, destination_root)
        self.stats["total"] = len(files)
        self.emit("scan_done", total=len(files), output_dir=str(destination_root))

        if not files:
            self.emit("job_done", stats=self.stats, cancelled=False)
            return

        with ThreadPoolExecutor(max_workers=cfg.workers) as executor:
            futures = [
                executor.submit(self._convert_one, f, destination_root, audio_args,
                                 out_ext, suffix)
                for f in files
            ]
            for future in as_completed(futures):
                result = future.result()
                with self._lock:
                    if result in self.stats:
                        self.stats[result] += 1
                    elif result == "dry":
                        self.stats.setdefault("dry", 0)
                        self.stats["dry"] += 1
                self.emit("progress", stats=dict(self.stats))

        self.emit("job_done", stats=dict(self.stats), cancelled=self._cancel.is_set())
