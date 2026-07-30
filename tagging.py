"""
One interface for reading and writing tags, cover art and lyrics, whatever
the container is.

Every format stores the same information under a different name and in a
different structure: FLAC and Ogg use Vorbis comments plus picture blocks,
MP3/AIFF/WAV use ID3 frames, and MP4 (AAC and ALAC) uses four-character
atoms. Rather than scatter that knowledge through the converter and the
library fixer, it lives here behind a small shared API:

    state = inspect(path)              # what's there, what's missing
    apply(path, fields=..., art=...)   # fill gaps, leave the rest alone

Everything is "only if missing" by default, because the common case is a
library that is mostly tagged correctly and the last thing anyone wants is
a tool that overwrites good data with a guess from the internet.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import mutagen
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import (
        APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, USLT,
    )
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
    MUTAGEN_AVAILABLE = True
except ImportError:  # pragma: no cover - mutagen is a hard requirement in practice
    MUTAGEN_AVAILABLE = False

# Fields FlacPress understands. Order matters only for display.
FIELDS = ("title", "artist", "albumartist", "album", "tracknumber",
          "tracktotal", "discnumber", "date", "genre")

# Fields whose absence counts as "this file needs fixing". tracktotal and
# discnumber are deliberately excluded: plenty of correctly tagged singles
# and single-disc albums simply don't have them.
CORE_FIELDS = ("title", "artist", "album", "albumartist", "tracknumber",
               "date", "genre")


def _first(value: Any) -> str:
    """Tag values come back as lists more often than not."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]).strip() if value else ""
    return str(value).strip()


def _split_pair(raw: str) -> tuple[str, str]:
    """'3/12' -> ('3', '12'). Also copes with a bare '3'."""
    text = _first(raw)
    if "/" in text:
        number, _, total = text.partition("/")
        return number.strip(), total.strip()
    return text, ""


def _year_of(raw: str) -> str:
    text = _first(raw)
    for index in range(len(text) - 3):
        chunk = text[index:index + 4]
        if chunk.isdigit():
            return chunk
    return text


def _image_format(data: bytes, mime: str = "") -> tuple[str, bool]:
    """Return (mime, is_png), sniffing the bytes rather than trusting mime."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", True
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", False
    if mime:
        return mime, "png" in mime.lower()
    return "image/jpeg", False


# --------------------------------------------------------------------------
# Per-container adapters
# --------------------------------------------------------------------------


class _Adapter:
    """Shared behaviour. Subclasses translate to and from one tag scheme."""

    container = "unknown"

    def __init__(self, audio, path: Path):
        self.audio = audio
        self.path = path
        self._dirty = False

    # -- reading

    def fields(self) -> Dict[str, str]:
        raise NotImplementedError

    def has_art(self) -> bool:
        raise NotImplementedError

    def has_lyrics(self) -> bool:
        raise NotImplementedError

    @property
    def duration(self) -> float:
        return float(getattr(getattr(self.audio, "info", None), "length", 0) or 0)

    # -- writing

    def set_field(self, name: str, value: str) -> bool:
        raise NotImplementedError

    def set_art(self, data: bytes, mime: str = "") -> bool:
        raise NotImplementedError

    def set_lyrics(self, text: str) -> bool:
        raise NotImplementedError

    def save(self) -> bool:
        if not self._dirty:
            return False
        self.audio.save()
        return True


class _VorbisAdapter(_Adapter):
    """FLAC, Ogg Vorbis and Opus: Vorbis comments, keys are case-insensitive."""

    container = "vorbis"
    KEYS = {
        "title": ("TITLE",), "artist": ("ARTIST",),
        "albumartist": ("ALBUMARTIST", "ALBUM ARTIST"), "album": ("ALBUM",),
        "tracknumber": ("TRACKNUMBER", "TRACK"),
        "tracktotal": ("TRACKTOTAL", "TOTALTRACKS"),
        "discnumber": ("DISCNUMBER", "DISC"),
        "date": ("DATE", "YEAR", "ORIGINALDATE"), "genre": ("GENRE",),
    }
    LYRIC_KEYS = ("LYRICS", "UNSYNCEDLYRICS", "SYNCEDLYRICS", "LYRICS:DESCRIPTION")

    def _lookup(self, names) -> str:
        for name in names:
            for key in (name, name.lower(), name.title()):
                if key in self.audio:
                    value = _first(self.audio[key])
                    if value:
                        return value
        return ""

    def fields(self) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for name, keys in self.KEYS.items():
            value = self._lookup(keys)
            if value:
                found[name] = value
        # a "3/12" style TRACKNUMBER carries the total with it
        if "tracknumber" in found:
            number, total = _split_pair(found["tracknumber"])
            found["tracknumber"] = number
            if total and not found.get("tracktotal"):
                found["tracktotal"] = total
        if "date" in found:
            found["date"] = _year_of(found["date"])
        return {key: value for key, value in found.items() if value}

    def has_art(self) -> bool:
        if isinstance(self.audio, FLAC):
            return bool(self.audio.pictures)
        return bool(self.audio.get("metadata_block_picture")
                    or self.audio.get("METADATA_BLOCK_PICTURE"))

    def has_lyrics(self) -> bool:
        return bool(self._lookup(self.LYRIC_KEYS))

    def set_field(self, name: str, value: str) -> bool:
        key = self.KEYS.get(name, (name.upper(),))[0]
        self.audio[key] = [value]
        self._dirty = True
        return True

    def _picture(self, data: bytes, mime: str) -> Picture:
        mime, _ = _image_format(data, mime)
        picture = Picture()
        picture.type = 3  # front cover
        picture.mime = mime
        picture.data = data
        picture.depth = 24
        return picture

    def set_art(self, data: bytes, mime: str = "") -> bool:
        picture = self._picture(data, mime)
        if isinstance(self.audio, FLAC):
            self.audio.clear_pictures()
            self.audio.add_picture(picture)
        else:
            # Ogg has no picture block, so the convention is a base64 blob
            self.audio["metadata_block_picture"] = [
                base64.b64encode(picture.write()).decode("ascii")]
        self._dirty = True
        return True

    def set_lyrics(self, text: str) -> bool:
        self.audio["LYRICS"] = [text]
        self._dirty = True
        return True


class _ID3Adapter(_Adapter):
    """MP3, AIFF and WAV all carry an ID3 chunk."""

    container = "id3"
    FRAMES = {
        "title": "TIT2", "artist": "TPE1", "albumartist": "TPE2",
        "album": "TALB", "discnumber": "TPOS", "date": "TDRC", "genre": "TCON",
    }
    BUILDERS = {
        "TIT2": TIT2, "TPE1": TPE1, "TPE2": TPE2, "TALB": TALB,
        "TPOS": TPOS, "TDRC": TDRC, "TCON": TCON,
    }

    def __init__(self, audio, path: Path):
        super().__init__(audio, path)
        if audio.tags is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        self.tags: Optional[ID3] = audio.tags

    def _text(self, frame_id: str) -> str:
        # `is None`, not falsiness: an ID3 object with no frames yet is
        # dict-like and therefore falsy, which would make a freshly created
        # tag chunk (WAV and AIFF start with none at all) look unusable.
        if self.tags is None:
            return ""
        frame = self.tags.get(frame_id)
        if frame is None:
            return ""
        return _first(getattr(frame, "text", ""))

    def fields(self) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for name, frame_id in self.FRAMES.items():
            value = self._text(frame_id)
            if value:
                found[name] = value
        number, total = _split_pair(self._text("TRCK"))
        if number:
            found["tracknumber"] = number
        if total:
            found["tracktotal"] = total
        if not found.get("date"):
            legacy = self._text("TYER") or self._text("TDAT")
            if legacy:
                found["date"] = legacy
        if "date" in found:
            found["date"] = _year_of(found["date"])
        if "discnumber" in found:
            found["discnumber"] = _split_pair(found["discnumber"])[0]
        return found

    def has_art(self) -> bool:
        return self.tags is not None and bool(self.tags.getall("APIC"))

    def has_lyrics(self) -> bool:
        if self.tags is None:
            return False
        return bool(self.tags.getall("USLT") or self.tags.getall("SYLT"))

    def set_field(self, name: str, value: str) -> bool:
        if self.tags is None:
            return False
        if name in ("tracknumber", "tracktotal"):
            current_number, current_total = _split_pair(self._text("TRCK"))
            number = value if name == "tracknumber" else current_number
            total = value if name == "tracktotal" else current_total
            if not number:
                return False
            self.tags.setall("TRCK", [TRCK(encoding=3,
                                           text=f"{number}/{total}" if total else number)])
        else:
            frame_id = self.FRAMES.get(name)
            builder = self.BUILDERS.get(frame_id or "")
            if not builder:
                return False
            self.tags.setall(frame_id, [builder(encoding=3, text=value)])
        self._dirty = True
        return True

    def set_art(self, data: bytes, mime: str = "") -> bool:
        if self.tags is None:
            return False
        mime, _ = _image_format(data, mime)
        self.tags.delall("APIC")
        self.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        self._dirty = True
        return True

    def set_lyrics(self, text: str) -> bool:
        if self.tags is None:
            return False
        self.tags.delall("USLT")
        self.tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
        self._dirty = True
        return True

    def save(self) -> bool:
        if not self._dirty:
            return False
        # v2.3 is the most widely readable flavour on older hardware
        try:
            self.audio.save(v2_version=3)
        except TypeError:
            self.audio.save()
        return True


class _MP4Adapter(_Adapter):
    """AAC and ALAC in an MP4 container."""

    container = "mp4"
    ATOMS = {
        "title": "\xa9nam", "artist": "\xa9ART", "albumartist": "aART",
        "album": "\xa9alb", "date": "\xa9day", "genre": "\xa9gen",
    }

    def fields(self) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for name, atom in self.ATOMS.items():
            value = _first(self.audio.get(atom))
            if value:
                found[name] = value
        track = self.audio.get("trkn")
        if track and track[0]:
            number, total = (list(track[0]) + [0, 0])[:2]
            if number:
                found["tracknumber"] = str(number)
            if total:
                found["tracktotal"] = str(total)
        disc = self.audio.get("disk")
        if disc and disc[0] and disc[0][0]:
            found["discnumber"] = str(disc[0][0])
        if "date" in found:
            found["date"] = _year_of(found["date"])
        return found

    def has_art(self) -> bool:
        return bool(self.audio.get("covr"))

    def has_lyrics(self) -> bool:
        return bool(_first(self.audio.get("\xa9lyr")))

    def set_field(self, name: str, value: str) -> bool:
        if name in ("tracknumber", "tracktotal"):
            existing = self.audio.get("trkn") or [(0, 0)]
            number, total = (list(existing[0]) + [0, 0])[:2]
            try:
                if name == "tracknumber":
                    number = int(value)
                else:
                    total = int(value)
            except ValueError:
                return False
            self.audio["trkn"] = [(number, total)]
        elif name == "discnumber":
            try:
                self.audio["disk"] = [(int(value), 0)]
            except ValueError:
                return False
        else:
            atom = self.ATOMS.get(name)
            if not atom:
                return False
            self.audio[atom] = [value]
        self._dirty = True
        return True

    def set_art(self, data: bytes, mime: str = "") -> bool:
        _, is_png = _image_format(data, mime)
        self.audio["covr"] = [MP4Cover(
            data, imageformat=MP4Cover.FORMAT_PNG if is_png else MP4Cover.FORMAT_JPEG)]
        self._dirty = True
        return True

    def set_lyrics(self, text: str) -> bool:
        self.audio["\xa9lyr"] = [text]
        self._dirty = True
        return True


def _adapter_for(path: Path) -> Optional[_Adapter]:
    if not MUTAGEN_AVAILABLE:
        return None
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None:
        return None
    if isinstance(audio, (FLAC, OggOpus, OggVorbis)):
        return _VorbisAdapter(audio, path)
    if isinstance(audio, MP4):
        return _MP4Adapter(audio, path)
    if isinstance(audio, (MP3, AIFF, WAVE)):
        return _ID3Adapter(audio, path)
    # Anything else with a tag interface: try Vorbis-style, it's the most
    # forgiving, and fall back to reporting nothing rather than crashing.
    if hasattr(audio, "get"):
        return _VorbisAdapter(audio, path)
    return None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def inspect(path: str | Path) -> Dict[str, Any]:
    """What tags, art and lyrics does this file already have?"""
    target = Path(path)
    result: Dict[str, Any] = {
        "ok": False, "container": "", "fields": {}, "missing": [],
        "has_art": False, "has_lyrics": False, "duration": 0.0, "error": "",
    }
    adapter = _adapter_for(target)
    if adapter is None:
        result["error"] = "unsupported or unreadable file"
        return result
    try:
        fields = adapter.fields()
        result.update({
            "ok": True,
            "container": adapter.container,
            "fields": fields,
            "missing": [name for name in CORE_FIELDS if not fields.get(name)],
            "has_art": adapter.has_art(),
            "has_lyrics": adapter.has_lyrics(),
            "duration": adapter.duration,
        })
    except Exception as exc:
        result["error"] = f"could not read tags: {exc}"
    return result


def apply(
    path: str | Path,
    fields: Optional[Dict[str, str]] = None,
    art: Optional[bytes] = None,
    art_mime: str = "",
    lyrics: Optional[str] = None,
    only_missing: bool = True,
) -> Dict[str, Any]:
    """Write whatever is supplied, skipping anything already present.

    Returns a record of what actually changed, so callers can report
    honestly instead of claiming success for a no-op.
    """
    target = Path(path)
    changed: Dict[str, Any] = {"ok": False, "fields": [], "art": False,
                               "lyrics": False, "error": ""}
    adapter = _adapter_for(target)
    if adapter is None:
        changed["error"] = "unsupported or unreadable file"
        return changed

    try:
        existing = adapter.fields()
        for name, value in (fields or {}).items():
            value = (value or "").strip()
            if not value or name not in FIELDS:
                continue
            if only_missing and existing.get(name):
                continue
            if adapter.set_field(name, value):
                changed["fields"].append(name)

        if art and not (only_missing and adapter.has_art()):
            if adapter.set_art(art, art_mime):
                changed["art"] = True

        if lyrics and not (only_missing and adapter.has_lyrics()):
            if adapter.set_lyrics(lyrics):
                changed["lyrics"] = True

        if changed["fields"] or changed["art"] or changed["lyrics"]:
            adapter.save()
        changed["ok"] = True
    except Exception as exc:
        changed["error"] = f"could not write tags: {exc}"
    return changed


def copy_all(source: str | Path, destination: str | Path,
             include_art: bool = True, include_lyrics: bool = True) -> Dict[str, Any]:
    """Carry tags (and optionally art and lyrics) from one file to another.

    Used after conversion: the encoded file starts blank, so here
    only_missing is irrelevant and everything is copied across.
    """
    state = inspect(source)
    if not state["ok"]:
        return {"ok": False, "fields": [], "art": False, "lyrics": False,
                "error": state["error"]}

    art_bytes = None
    art_mime = ""
    if include_art and state["has_art"]:
        art_bytes, art_mime = read_art(source)

    lyrics = None
    if include_lyrics and state["has_lyrics"]:
        lyrics = read_lyrics(source)

    return apply(destination, fields=state["fields"], art=art_bytes,
                 art_mime=art_mime, lyrics=lyrics, only_missing=False)


def read_art(path: str | Path) -> tuple[Optional[bytes], str]:
    """Pull the embedded front cover out of a file, if there is one."""
    adapter = _adapter_for(Path(path))
    if adapter is None:
        return None, ""
    try:
        audio = adapter.audio
        if isinstance(audio, FLAC) and audio.pictures:
            picture = next((p for p in audio.pictures if p.type == 3), audio.pictures[0])
            return bytes(picture.data), picture.mime
        if isinstance(audio, MP4):
            covers = audio.get("covr")
            if covers:
                cover = covers[0]
                is_png = getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG
                return bytes(cover), "image/png" if is_png else "image/jpeg"
        tags = getattr(audio, "tags", None)
        if tags is not None and hasattr(tags, "getall"):
            frames = tags.getall("APIC")
            if frames:
                frame = next((f for f in frames if f.type == 3), frames[0])
                return bytes(frame.data), frame.mime
        blobs = audio.get("metadata_block_picture") or audio.get(
            "METADATA_BLOCK_PICTURE") if hasattr(audio, "get") else None
        if blobs:
            picture = Picture(base64.b64decode(blobs[0]))
            return bytes(picture.data), picture.mime
    except Exception:
        return None, ""
    return None, ""


def read_lyrics(path: str | Path) -> Optional[str]:
    adapter = _adapter_for(Path(path))
    if adapter is None:
        return None
    try:
        audio = adapter.audio
        tags = getattr(audio, "tags", None)
        if tags is not None and hasattr(tags, "getall"):
            frames = tags.getall("USLT")
            if frames:
                return str(frames[0].text)
        if isinstance(audio, MP4):
            value = _first(audio.get("\xa9lyr"))
            return value or None
        if hasattr(audio, "get"):
            for key in _VorbisAdapter.LYRIC_KEYS:
                for variant in (key, key.lower()):
                    if variant in audio:
                        return _first(audio[variant]) or None
    except Exception:
        return None
    return None


def write_lrc_sidecar(audio_path: str | Path, lrc: str,
                      overwrite: bool = False) -> bool:
    """Write a .lrc file next to the track.

    Belt and braces: embedded lyrics are tidier, but a sidecar .lrc is the
    one thing essentially every player understands.
    """
    target = Path(audio_path).with_suffix(".lrc")
    if target.exists() and not overwrite:
        return False
    try:
        target.write_text(lrc, encoding="utf-8")
        return True
    except OSError:
        return False


def has_sidecar_lrc(audio_path: str | Path) -> bool:
    return Path(audio_path).with_suffix(".lrc").is_file()
