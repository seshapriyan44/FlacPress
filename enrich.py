"""
The library fixer: fills in missing tags, cover art and synced lyrics on
files where they are, without converting anything.

This is deliberately separate from ConversionJob. Converting produces a new
file, so writing tags to it is free of risk. Fixing a library edits the
originals, which is a different promise entirely, so:

  * only missing information is ever written - existing tags are never
    overwritten (there is a flag to force it, off by default and not
    exposed in the UI's normal path)
  * preview mode is the default, so you see the proposed changes before
    anything touches a file
  * audio data is never rewritten. Only the tag block is updated, by
    mutagen, in place

Where the information comes from, in order of preference:

  1. what the file already has                   (kept, never replaced)
  2. a cover image sitting in the album folder   (free, offline, and
                                                  already the user's choice)
  3. an online lookup                            (iTunes / Deezer /
                                                  Cover Art Archive / LRCLIB)
  4. the file and folder names                   (offline fallback, and
                                                  usually the best source of
                                                  artist and title)
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import providers
import tagging

# Every audio format worth fixing, not just the lossless ones - people want
# their MP3 collection tidied up too.
FIXABLE_EXTENSIONS = {
    ".flac", ".wav", ".wave", ".aiff", ".aif", ".aifc", ".ape", ".alac",
    ".m4a", ".m4b", ".wv", ".tta", ".tak", ".dsf", ".dff", ".mp3", ".ogg",
    ".oga", ".opus", ".aac", ".wma", ".mpc",
}

COVER_FILENAMES = ("cover", "folder", "front", "album", "albumart",
                   "albumartsmall", "artwork", "thumb", "cover_art")
COVER_SUFFIXES = (".jpg", ".jpeg", ".png")
MAX_LOCAL_COVER = 8 * 1024 * 1024

# Folder names that say nothing about the music inside them, so they must
# never be mistaken for an artist or album name.
GENERIC_FOLDERS = {
    "music", "musik", "songs", "song", "audio", "albums", "album", "tracks",
    "downloads", "download", "new folder", "unsorted", "misc", "various",
    "various artists", "va", "compilations", "flac", "mp3", "opus", "aac",
    "lossless", "media", "library", "itunes", "desktop", "documents", "temp",
    "tmp", "cd", "cd1", "cd2", "disc", "disc1", "disc2", "disk", "rips",
    "converted", "sorted", "singles", "playlist", "playlists", "shared",
    "lib", "libraries", "collection", "collections", "artists", "files",
    "folder", "new", "backup", "backups", "onedrive", "dropbox",
    "google drive", "recordings", "users", "home", "volumes", "mnt",
}
# Note: the folder above an album is treated as the artist unless its name is
# in that list. It's a guess, so it only ever fills a field that is already
# empty, and an online match takes precedence over it.

YEAR = re.compile(r"(?:19|20)\d{2}")

# Tried in order against the file's stem.
NAME_PATTERNS = (
    # 1-05 - Artist - Title   /   05 - Artist - Title
    re.compile(r"^(?:\d{1,2}[-.]\s*)?(?P<track>\d{1,3})\s*[-–_.)\]]\s*"
               r"(?P<artist>.{2,60}?)\s+[-–]\s+(?P<title>.+)$"),
    # 05 Artist - Title  (no punctuation after the track number)
    re.compile(r"^(?P<track>\d{1,3})\s+(?P<artist>.{2,60}?)\s+[-–]\s+"
               r"(?P<title>.+)$"),
    # 05 - Title  /  05. Title  /  05) Title  /  05_Title
    re.compile(r"^(?:\d{1,2}[-.]\s*)?(?P<track>\d{1,3})\s*[-–_.)\]]\s*"
               r"(?P<title>.+)$"),
    # 05 Title
    re.compile(r"^(?P<track>\d{1,3})\s+(?P<title>.{2,})$"),
    # Artist - Title
    re.compile(r"^(?P<artist>.{2,60}?)\s+[-–]\s+(?P<title>.+)$"),
)

# "2001 - Album", "Album (2001)", "[2001] Album"
FOLDER_YEAR_PATTERNS = (
    re.compile(r"^\s*[\[\(]?(?P<year>(?:19|20)\d{2})[\]\)]?\s*[-–.]?\s*(?P<album>.+)$"),
    re.compile(r"^(?P<album>.+?)\s*[\[\(](?P<year>(?:19|20)\d{2})[\]\)]\s*$"),
)


def _tidy(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").replace("_", " ")).strip()
    return cleaned.strip(" -–.")


def _is_generic(name: str) -> bool:
    return _tidy(name).lower() in GENERIC_FOLDERS


def infer_from_path(path: Path) -> Dict[str, str]:
    """Guess artist / album / title / track / year from names on disk.

    Filenames are frequently the only place this information survives, and
    for artist and title they're usually more reliable than a fuzzy online
    search. Nothing here is certain, so results only ever fill gaps.
    """
    guessed: Dict[str, str] = {}
    stem = _tidy(path.stem)

    for pattern in NAME_PATTERNS:
        match = pattern.match(stem)
        if not match:
            continue
        parts = match.groupdict()
        title = _tidy(parts.get("title") or "")
        if not title:
            continue
        guessed["title"] = title
        track = (parts.get("track") or "").lstrip("0")
        if track:
            guessed["tracknumber"] = track
        artist = _tidy(parts.get("artist") or "")
        if artist and not _is_generic(artist):
            guessed["artist"] = artist
        break
    else:
        if stem:
            guessed["title"] = stem

    # Album from the containing folder, artist from the one above it.
    album_dir = path.parent
    if album_dir and album_dir.name and not _is_generic(album_dir.name):
        album_name = _tidy(album_dir.name)
        for pattern in FOLDER_YEAR_PATTERNS:
            match = pattern.match(album_name)
            if match:
                album_name = _tidy(match.group("album"))
                guessed["date"] = match.group("year")
                break
        # "Artist - Album" as a single folder name
        if " - " in album_name and "artist" not in guessed:
            left, _, right = album_name.partition(" - ")
            if len(left) > 1 and len(right) > 1:
                guessed.setdefault("artist", _tidy(left))
                album_name = _tidy(right)
        if album_name:
            guessed["album"] = album_name

    # The folder above the album is usually the artist. This deliberately
    # looks above the scan root too: pointing FlacPress at a single album
    # folder is the most common way to use it, and in that case the artist
    # name only exists one level up. Drive roots have an empty .name, and
    # anything uninformative is caught by the generic-name list.
    artist_dir = album_dir.parent if album_dir else None
    if (artist_dir and artist_dir.name and not _is_generic(artist_dir.name)
            and "artist" not in guessed):
        guessed["artist"] = _tidy(artist_dir.name)

    if not guessed.get("date"):
        # A year in the album folder that didn't match the patterns above
        match = YEAR.search(album_dir.name if album_dir else "")
        if match:
            guessed["date"] = match.group(0)

    return {key: value for key, value in guessed.items() if value}


def find_local_cover(folder: Path) -> tuple[Optional[bytes], str]:
    """Look for cover art already sitting in the album folder."""
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return None, ""
    candidates = []
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() not in COVER_SUFFIXES:
            continue
        stem = entry.stem.lower().replace(" ", "").replace("_", "")
        if stem in COVER_FILENAMES:
            candidates.insert(0, entry)
        elif any(stem.startswith(name) for name in COVER_FILENAMES):
            candidates.append(entry)
    for candidate in candidates:
        try:
            if candidate.stat().st_size > MAX_LOCAL_COVER:
                continue
            data = candidate.read_bytes()
        except OSError:
            continue
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return data, "image/png"
        if data[:3] == b"\xff\xd8\xff":
            return data, "image/jpeg"
    return None, ""


def scan_fixable(source_dir: Path, recursive: bool = True,
                 extensions=FIXABLE_EXTENSIONS) -> List[Path]:
    root = Path(source_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    walker = root.rglob("*") if recursive else root.glob("*")
    found = []
    for path in walker:
        try:
            if path.is_file() and path.suffix.lower() in extensions:
                found.append(path)
        except OSError:
            continue
    return sorted(found)


@dataclass
class EnrichConfig:
    source_dir: Path
    fix_metadata: bool = True
    fix_art: bool = True
    fix_lyrics: bool = True
    use_online: bool = True
    infer_from_names: bool = True
    use_local_cover: bool = True
    lrc_sidecar: bool = False
    art_size: int = 1000
    recursive: bool = True
    workers: int = field(default_factory=lambda: max(1, min(4, (os.cpu_count() or 4) - 2)))
    dry_run: bool = True          # preview unless the caller says otherwise
    overwrite_existing: bool = False   # off by default, and it should stay off

    def __post_init__(self):
        # Same normalisation trap as JobConfig: scan results are absolute,
        # so the configured root has to be too.
        self.source_dir = Path(self.source_dir).expanduser().resolve()


def describe_plan(plan: Dict[str, Any]) -> str:
    parts = []
    if plan["fields"]:
        parts.append("tags: " + ", ".join(
            f"{key}={value}" for key, value in list(plan["fields"].items())[:6]))
    if plan["art"]:
        parts.append(f"art from {plan['art_source']} "
                     f"({len(plan['art']) // 1024} KB)")
    if plan["lyrics"]:
        lines = plan["lyrics"].count("\n") + 1
        parts.append(f"synced lyrics from {plan['lyrics_source']} ({lines} lines)")
    return " · ".join(parts)

def build_plan(path: Path, state: Dict[str, Any], config: EnrichConfig,
               name_source: Optional[Path] = None) -> Dict[str, Any]:
    """Work out what this file is missing and where to get it.

    name_source lets the caller point the filename guesser at a different
    path than the one being written. The converter needs that: its output
    sits in a folder with the format name appended ("Discovery OPUS"),
    which would otherwise be read back as the album title.
    """
    cfg = config
    existing = state["fields"]
    missing = list(state["missing"])
    plan: Dict[str, Any] = {
        "fields": {}, "art": None, "art_mime": "", "art_source": "",
        "lyrics": None, "lyrics_source": "", "sources": [],
    }

    inferred: Dict[str, str] = {}
    if cfg.infer_from_names:
        inferred = infer_from_path(name_source or path)

    # Artist and title are what every lookup keys off, so fall back to
    # the filename guess when the tags don't have them.
    artist = existing.get("artist") or inferred.get("artist", "")
    title = existing.get("title") or inferred.get("title", "")
    album = existing.get("album") or inferred.get("album", "")

    online: Dict[str, str] = {}
    if cfg.fix_metadata and missing and cfg.use_online and title:
        facts = providers.find_track_metadata(artist, title, album,
                                              state["duration"])
        if facts:
            online = facts.fields
            plan["sources"].append(f"{facts.source} ({facts.confidence:.2f})")

    if cfg.fix_metadata:
        for name in missing:
            value = online.get(name) or inferred.get(name, "")
            if value:
                plan["fields"][name] = value
        if inferred and not online and plan["fields"]:
            plan["sources"].append("file names")

    # Refresh what we know now that gaps may be filled - the art and
    # lyrics lookups want the best available artist/album/title.
    artist = existing.get("artist") or plan["fields"].get("artist", "")
    album = existing.get("album") or plan["fields"].get("album", "")
    title = existing.get("title") or plan["fields"].get("title", "")
    albumartist = existing.get("albumartist") or plan["fields"].get("albumartist", "")

    if cfg.fix_art and not state["has_art"]:
        if cfg.use_local_cover:
            data, mime = find_local_cover(path.parent)
            if data:
                plan.update({"art": data, "art_mime": mime,
                             "art_source": "album folder"})
        if plan["art"] is None and cfg.use_online and (album or title):
            art = providers.find_cover_art(albumartist or artist, album,
                                           cfg.art_size, title=title)
            if art:
                plan.update({"art": art.data, "art_mime": art.mime,
                             "art_source": art.source})

    if cfg.fix_lyrics and not state["has_lyrics"] and cfg.use_online:
        if artist and title:
            lrc = providers.find_synced_lyrics(artist, title, album,
                                              state["duration"])
            if lrc:
                plan["lyrics"] = lrc
                plan["lyrics_source"] = "LRCLIB"

    return plan


def enrich_file(path: Path, config: EnrichConfig,
                name_source: Optional[Path] = None) -> Dict[str, Any]:
    """Fix a single file. Used by the converter on its freshly written output.

    Returns what changed, including a short human-readable ``detail`` so the
    caller can report it, or an empty result when there was nothing to do.
    """
    outcome: Dict[str, Any] = {"ok": False, "fields": [], "art": False,
                              "lyrics": False, "detail": "", "error": ""}
    state = tagging.inspect(path)
    if not state["ok"]:
        outcome["error"] = state["error"]
        return outcome

    plan = build_plan(Path(path), state, config, name_source=name_source)
    if not (plan["fields"] or plan["art"] or plan["lyrics"]):
        outcome["ok"] = True
        return outcome

    if config.dry_run:
        outcome.update({"ok": True, "detail": describe_plan(plan)})
        return outcome

    result = tagging.apply(path, fields=plan["fields"], art=plan["art"],
                           art_mime=plan["art_mime"], lyrics=plan["lyrics"],
                           only_missing=not config.overwrite_existing)
    if not result["ok"]:
        outcome["error"] = result["error"]
        return outcome

    if plan["lyrics"] and config.lrc_sidecar:
        tagging.write_lrc_sidecar(path, plan["lyrics"])

    pieces = []
    if result["fields"]:
        pieces.append(", ".join(result["fields"]))
    if result["art"]:
        pieces.append(f"cover art ({plan['art_source']})")
    if result["lyrics"]:
        pieces.append("synced lyrics")
    outcome.update({"ok": True, "fields": result["fields"], "art": result["art"],
                    "lyrics": result["lyrics"], "detail": " · ".join(pieces)})
    return outcome


class EnrichJob:
    """Walks a folder and fills in what's missing, file by file.

    Emits the same event shape as ConversionJob so the existing
    Server-Sent-Events plumbing and UI work unchanged.
    """

    def __init__(self, config: EnrichConfig, on_event: Callable[[dict], None]):
        self.config = config
        self.on_event = on_event
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._slots: "queue.Queue[int]" = queue.Queue()
        for index in range(max(1, config.workers)):
            self._slots.put(index)
        self.stats = {
            "fixed": 0, "skipped": 0, "failed": 0, "dry": 0, "total": 0,
            "tags_added": 0, "art_added": 0, "lyrics_added": 0,
        }

    def cancel(self):
        self._cancel.set()

    def emit(self, event_type: str, **data):
        self.on_event({"type": event_type, "ts": time.time(), **data})

    # ------------------------------------------------------------ per file

    def _plan(self, path: Path, state: Dict[str, Any]) -> Dict[str, Any]:
        return build_plan(path, state, self.config)


    def _process(self, path: Path) -> str:
        if self._cancel.is_set():
            return "cancelled"
        slot = self._slots.get()
        try:
            relative = path.relative_to(self.config.source_dir)
        except ValueError:
            relative = path
        try:
            self.emit("file_start", slot=slot, file=str(relative))

            state = tagging.inspect(path)
            if not state["ok"]:
                self.emit("file_done", slot=slot, file=str(relative),
                          status="failed", detail=state["error"])
                return "failed"

            nothing_missing = (
                (not self.config.fix_metadata or not state["missing"])
                and (not self.config.fix_art or state["has_art"])
                and (not self.config.fix_lyrics or state["has_lyrics"]))
            if nothing_missing and not self.config.overwrite_existing:
                self.emit("file_done", slot=slot, file=str(relative),
                          status="skipped", detail="nothing missing")
                return "skipped"

            plan = self._plan(path, state)
            if self._cancel.is_set():
                return "cancelled"

            if not (plan["fields"] or plan["art"] or plan["lyrics"]):
                gaps = []
                if state["missing"]:
                    gaps.append("tags: " + ", ".join(state["missing"]))
                if self.config.fix_art and not state["has_art"]:
                    gaps.append("art")
                if self.config.fix_lyrics and not state["has_lyrics"]:
                    gaps.append("lyrics")
                self.emit("file_done", slot=slot, file=str(relative),
                          status="skipped",
                          detail="nothing found for " + "; ".join(gaps)
                                 if gaps else "nothing missing")
                return "skipped"

            summary = describe_plan(plan)

            if self.config.dry_run:
                self.emit("file_done", slot=slot, file=str(relative),
                          status="dry", detail=summary)
                return "dry"

            result = tagging.apply(
                path, fields=plan["fields"], art=plan["art"],
                art_mime=plan["art_mime"], lyrics=plan["lyrics"],
                only_missing=not self.config.overwrite_existing)
            if not result["ok"]:
                self.emit("file_done", slot=slot, file=str(relative),
                          status="failed", detail=result["error"])
                return "failed"

            if plan["lyrics"] and self.config.lrc_sidecar:
                tagging.write_lrc_sidecar(path, plan["lyrics"])

            with self._lock:
                self.stats["tags_added"] += len(result["fields"])
                self.stats["art_added"] += 1 if result["art"] else 0
                self.stats["lyrics_added"] += 1 if result["lyrics"] else 0

            if not (result["fields"] or result["art"] or result["lyrics"]):
                self.emit("file_done", slot=slot, file=str(relative),
                          status="skipped", detail="already up to date")
                return "skipped"

            self.emit("file_done", slot=slot, file=str(relative),
                      status="fixed", detail=summary)
            return "fixed"
        except Exception as exc:
            self.emit("file_done", slot=slot, file=str(relative),
                      status="failed", detail=str(exc))
            return "failed"
        finally:
            self._slots.put(slot)

    # ----------------------------------------------------------------- run

    def run(self):
        cfg = self.config
        if not cfg.source_dir.is_dir():
            self.emit("error", detail=f"'{cfg.source_dir}' is not a folder.")
            return
        if not tagging.MUTAGEN_AVAILABLE:
            self.emit("error", detail="mutagen is required to read and write tags.")
            return

        if cfg.use_online:
            providers.network.reset()

        files = scan_fixable(cfg.source_dir, cfg.recursive)
        self.stats["total"] = len(files)
        self.emit("scan_done", total=len(files), output_dir=str(cfg.source_dir),
                  dry_run=cfg.dry_run)
        if not files:
            self.emit("job_done", stats=dict(self.stats), cancelled=False)
            return

        with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as executor:
            futures = [executor.submit(self._process, path) for path in files]
            for future in as_completed(futures):
                outcome = future.result()
                with self._lock:
                    if outcome in self.stats:
                        self.stats[outcome] += 1
                self.emit("progress", stats=dict(self.stats))

        if cfg.use_online and not providers.network.available:
            self.emit("warning", detail="Lookups stopped early: no internet "
                                        f"connection ({providers.network.reason}).")
        self.emit("job_done", stats=dict(self.stats),
                  cancelled=self._cancel.is_set())
