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
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

try:
    import mutagen
    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3NoHeaderError
    from mutagen.easymp4 import EasyMP4
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

# ==========================
# Format + bitrate presets
# ==========================

LOSSLESS_EXTENSIONS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".alac", ".m4a", ".wv"}

FORMAT_PRESETS = {
    "opus": {
        "ext": ".opus",
        "base_args": ["-c:a", "libopus", "-vbr", "on"],
        # Ogg has no stream-copy equivalent for cover art (unlike MP4/ID3
        # containers) — art is embedded as a METADATA_BLOCK_PICTURE tag in
        # a post-processing step instead. See embed_ogg_cover().
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
}

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


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
    return [*preset["base_args"], opt["flag"], opt["value"]], preset["ext"]


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
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
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
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
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


# ==========================
# Metadata — copied via mutagen, not ffmpeg
# ==========================
# Verified against a real ffmpeg build: some ffmpeg Ogg muxers silently
# write zero metadata for Opus/Vorbis output (not even a hardcoded
# -metadata flag survives), and even where ffmpeg's mapping does work,
# multi-valued fields like a second contributing artist get flattened
# into "Artist A;Artist B" as one string instead of two real values.
# mutagen's Easy* wrappers read/write both correctly across formats, so
# they're used as the single source of truth for every format's tags —
# not just Opus's — via mutagen.File(src, easy=True) as the reader.

def copy_tags(dst: Path, src: Path, fmt: str) -> int:
    """Copies text tags (title, artist, album, album artist, track/disc
    number, date, genre, composer, ...) from src into dst. Returns the
    number of fields copied (0 if nothing to copy or mutagen isn't
    installed). Never raises — a metadata miss shouldn't fail the file."""
    if not MUTAGEN_AVAILABLE:
        return 0
    try:
        source = mutagen.File(src, easy=True)
    except Exception:
        return 0
    if not source or not source.tags:
        return 0

    try:
        if fmt == "opus":
            target = OggOpus(dst)
        elif fmt == "mp3":
            try:
                target = EasyID3(dst)
            except ID3NoHeaderError:
                target = EasyID3()
        elif fmt == "aac":
            target = EasyMP4(dst)
        else:
            return 0
    except Exception:
        return 0

    count = 0
    for key, value in source.tags.items():
        if not value:
            continue
        try:
            target[key.upper() if fmt == "opus" else key] = value
            count += 1
        except Exception:
            continue  # this field isn't supported by the target format's tag scheme — skip it

    try:
        target.save(dst)
    except Exception:
        return 0
    return count


# ==========================
# Cover art for Ogg-based formats (Opus)
# ==========================
# Ogg containers have no "attached video stream" the way MP4/ID3 do, so
# ffmpeg's usual `-map 0 -c:v copy` trick can't carry cover art into a
# .opus file. The real Ogg/Vorbis convention is a FLAC-style Picture
# block, base64-encoded into a METADATA_BLOCK_PICTURE tag. mutagen builds
# that block correctly and writes it straight into the file after
# encoding.

_MIME_BY_CODEC = {"mjpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "bmp": "image/bmp"}


def probe_cover_stream(file: Path) -> Optional[dict]:
    """Returns {codec_name, width, height} for the embedded cover-art
    stream (ffmpeg exposes it as an attached_pic video stream), or None."""
    try:
        result = subprocess.run(
            [
                FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height", "-of", "json", str(file),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        data = json.loads(result.stdout or b"{}")
        streams = data.get("streams") or []
        return streams[0] if streams else None
    except Exception:
        return None


def extract_cover_bytes(file: Path) -> Optional[bytes]:
    """Pulls the embedded cover image out via ffmpeg straight to memory —
    no temp file needed."""
    try:
        result = subprocess.run(
            [
                FFMPEG, "-v", "error", "-i", str(file), "-an", "-map", "0:v:0",
                "-c", "copy", "-f", "image2pipe", "-frames:v", "1", "pipe:1",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return None


def embed_ogg_cover(dst: Path, src: Path) -> bool:
    """Best-effort: copies src's embedded cover art into an already-encoded
    Ogg Opus file at dst. Returns True if art was embedded, False if there
    was nothing to embed or mutagen isn't installed — either way this
    never raises, since a missing cover shouldn't fail the conversion."""
    if not MUTAGEN_AVAILABLE:
        return False
    stream = probe_cover_stream(src)
    if not stream:
        return False
    mime = _MIME_BY_CODEC.get(stream.get("codec_name"))
    if not mime:
        return False
    image_bytes = extract_cover_bytes(src)
    if not image_bytes:
        return False
    try:
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = mime
        pic.data = image_bytes
        pic.width = stream.get("width") or 0
        pic.height = stream.get("height") or 0
        pic.depth = 24

        audio = OggOpus(dst)
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        audio.save()
        return True
    except Exception:
        return False


@dataclass
class JobConfig:
    source_dir: Path
    destination_dir: Optional[Path] = None  # None => source_dir.parent (sibling of source)
    output_format: str = "opus"
    bitrate: Optional[str] = None   # bitrate preset id, e.g. "160k" or "v0" — see FORMAT_PRESETS
    workers: int = field(default_factory=lambda: max(1, (os.cpu_count() or 4) - 2))
    dry_run: bool = False
    force: bool = False           # re-convert even if destination already exists
    verify: bool = True           # ffprobe-validate every output file
    skip_lossy_m4a: bool = True   # skip .m4a sources that are already lossy AAC
    embed_cover_art: bool = True  # for Ogg-based formats, embed art via post-processing


def _build_command(src: Path, dst: Path, audio_args: list[str], include_cover: bool) -> list[str]:
    command = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src),
        *audio_args,
        "-map_metadata", "0",
    ]
    if include_cover:
        command += ["-map", "0", "-c:v", "copy"]
    else:
        command += ["-map", "0:a"]
    command += [str(dst)]
    return command


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
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self._lock:
            self._active_procs[slot] = proc
        try:
            _, stderr = proc.communicate()
        finally:
            with self._lock:
                self._active_procs.pop(slot, None)
        return proc.returncode, stderr

    def _convert_one(self, src: Path, destination_root: Path, audio_args: list[str],
                      out_ext: str, suffix: str, embed_cover: bool, ogg_container: bool) -> str:
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

            command = _build_command(src, dst, audio_args, embed_cover)
            returncode, stderr = self._run_ffmpeg(slot, command)

            if self._cancel.is_set():
                dst.unlink(missing_ok=True)
                return "cancelled"

            fallback_note = ""
            if returncode != 0 and embed_cover:
                dst.unlink(missing_ok=True)
                command = _build_command(src, dst, audio_args, include_cover=False)
                returncode, stderr = self._run_ffmpeg(slot, command)
                fallback_note = "cover art dropped (source image incompatible with output container)"

            if self._cancel.is_set():
                dst.unlink(missing_ok=True)
                return "cancelled"

            if returncode != 0:
                dst.unlink(missing_ok=True)
                self.emit("file_done", slot=slot, file=str(relative), status="failed",
                           detail=(fallback_note + " " if fallback_note else "")
                                  + stderr.decode(errors="ignore")[-400:].strip())
                return "failed"

            if self.config.verify and not verify_output(dst):
                dst.unlink(missing_ok=True)
                self.emit("file_done", slot=slot, file=str(relative), status="failed", detail="verify failed")
                return "failed"

            # Metadata and (for Ogg) cover art are handled by mutagen after
            # encoding rather than trusted to ffmpeg's -map_metadata — see
            # the module docstring for why.
            copy_tags(dst, src, self.config.output_format)

            cover_note = ""
            if ogg_container and self.config.embed_cover_art:
                if not MUTAGEN_AVAILABLE:
                    cover_note = "cover art skipped (mutagen not installed)"
                elif embed_ogg_cover(dst, src):
                    cover_note = "cover art embedded"
                # else: no embedded art in source, or embedding failed — silent, not an error

            detail = " · ".join(d for d in (fallback_note, cover_note) if d) or None
            self.emit("file_done", slot=slot, file=str(relative), status="converted", detail=detail)
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
                                       f"Install ffmpeg and make sure it's on PATH.")
            return

        preset = FORMAT_PRESETS[cfg.output_format]
        audio_args, out_ext = build_audio_args(cfg.output_format, cfg.bitrate)
        embed_cover = preset["embed_cover"]
        ogg_container = preset.get("ogg_container", False)
        suffix = cfg.output_format.upper()

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
                                 out_ext, suffix, embed_cover, ogg_container)
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
