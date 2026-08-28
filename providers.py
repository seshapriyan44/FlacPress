"""
Online sources for cover art, synced lyrics and missing metadata.

Sources, all of which work without an API key or a signup:

  * iTunes Search  - cover art up to 3000px, plus per-track metadata
                     (track/disc numbers, release year, genre)
  * Deezer         - album cover art at 1000px, and a track-level search for
                     when the album name is missing or wrong
  * MusicBrainz +
    Cover Art Archive - the community database, tried by release and then by
                     release group; slower, but often the only source with
                     art for older or obscure records
  * LRCLIB         - timestamped .lrc lyrics

Cover art is tried in that order and the first usable image wins, so a
lookup normally costs one request. Results are cached per album.

Two sources that were considered and left out, with reasons:

  * covers.musichoarders.xyz has no public API - its /api endpoints answer
    401 Unauthorized. It's a browser front end that searches iTunes, Deezer,
    Bandcamp and the Cover Art Archive on your behalf, so querying those
    directly (as this module does) reaches the same images through
    documented interfaces instead of scraping a site that hasn't invited it.
  * Discogs search does answer without a token, but every cover_image field
    comes back empty unless you authenticate, so it would contribute nothing.
    Adding it later means shipping a personal token, which a distributed
    desktop app can't do safely.

Everything degrades quietly. No network, a blocked firewall or a service
having a bad day results in "nothing found", never an exception - and
after a few connection-level failures lookups short-circuit entirely, so
a 500-file run doesn't sit through 500 timeouts.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

try:
    from . import __version__  # pragma: no cover - kept for packaging flexibility
except ImportError:
    __version__ = "1.1.0"

USER_AGENT = (f"FlacPress/{__version__} "
              "(+https://github.com/seshapriyan44/FlacPress)")

TIMEOUT = 15
MAX_ART_BYTES = 8 * 1024 * 1024
MIN_ART_BYTES = 3 * 1024        # anything smaller is a placeholder, not artwork
ART_CACHE_LIMIT = 96

# Minimum interval between requests to the same host, in seconds.
# MusicBrainz asks for no more than one request per second and means it.
THROTTLES = {
    "musicbrainz.org": 1.1,
    "coverartarchive.org": 0.4,
    "itunes.apple.com": 0.25,
    "api.deezer.com": 0.25,
    "lrclib.net": 0.25,
}
DEFAULT_THROTTLE = 0.3


# --------------------------------------------------------------- networking


class _Network:
    """Tracks whether the internet appears to be reachable at all."""

    def __init__(self, give_up_after: int = 4):
        self.give_up_after = give_up_after
        self._failures = 0
        self._lock = threading.Lock()
        self.disabled = False
        self.reason = ""

    def record_failure(self, reason: str) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.give_up_after and not self.disabled:
                self.disabled = True
                self.reason = reason

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self.disabled = False
            self.reason = ""

    @property
    def available(self) -> bool:
        return not self.disabled


network = _Network()

_throttle_lock = threading.Lock()
_last_call: Dict[str, float] = {}


def _wait_turn(host: str) -> None:
    minimum = THROTTLES.get(host, DEFAULT_THROTTLE)
    while True:
        with _throttle_lock:
            now = time.monotonic()
            previous = _last_call.get(host, 0.0)
            wait = previous + minimum - now
            if wait <= 0:
                _last_call[host] = now
                return
        time.sleep(min(wait, 2.0))


def _request(url: str, accept: str = "application/json") -> Optional[bytes]:
    """GET a URL politely. Returns None for any failure at all."""
    if not network.available:
        return None
    host = urllib.parse.urlparse(url).netloc.lower()
    _wait_turn(host)
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_ART_BYTES + 1)
        network.record_success()
        return body
    except urllib.error.HTTPError:
        # The service answered, it just had nothing for us. Not a network
        # problem, so it must not count towards giving up.
        network.record_success()
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        network.record_failure(str(getattr(exc, "reason", exc)))
        return None
    except Exception:
        return None


def _get_json(url: str) -> Optional[Any]:
    body = _request(url)
    if not body:
        return None
    try:
        # iTunes serves JSON as text/javascript, so the content type is
        # deliberately not consulted here.
        return json.loads(body)
    except ValueError:
        return None


# ----------------------------------------------------------------- matching

# Matches a bracketed aside that *contains* any of these words anywhere, not
# just at the start: "(2021 Remastered Version)" is the common shape and it
# begins with the year. Only ever used for comparison, never for writing
# tags, so being heavy-handed here is safe.
_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(?:feat\.?|ft\.?|remaster(?:ed)?|deluxe|expanded|"
    r"bonus|explicit|clean|mono|stereo|anniversary|edition|version|re-?issue|"
    r"radio edit|single edit)\b[^\)\]]*[\)\]]", re.IGNORECASE)
_TRAILING = re.compile(r"\s*-\s*(?:remaster(?:ed)?|single|ep)\b.*$", re.IGNORECASE)


def simplify(text: str) -> str:
    """Reduce a title to something comparable across services."""
    cleaned = _NOISE.sub("", text or "")
    cleaned = _TRAILING.sub("", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _similarity_core(left: str, right: str) -> float:
    a, b = simplify(left), simplify(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        # One string containing the other is a strong signal ONLY when the
        # shorter one is a substantial fraction of the longer one's length.
        # Without a length check here, a generic short word ("Silence")
        # counts as a near-perfect match against any longer title that
        # happens to contain it ("Sound of Silence" — a different album by
        # a different artist entirely), which is exactly the kind of
        # false-positive that shipped wrong cover art. SequenceMatcher's
        # own ratio() isn't a safe fallback either — it naturally scores a
        # short substring embedded in a long string as fairly similar too,
        # for the same reason — so the containment case is scored purely
        # from length coverage instead of deferring to it.
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        coverage = len(shorter) / len(longer)
        if coverage >= 0.6:
            return 0.92
        return 0.3 + 0.35 * coverage   # e.g. "Silence" in "Sound of Silence" (~0.41 coverage) -> ~0.44
    return SequenceMatcher(None, a, b).ratio()


def similarity(left: str, right: str) -> float:
    best = _similarity_core(left, right)
    # Segued/medley tracks are commonly catalogued as "X / Y" ("House Of
    # Balloons / Glass Table Girls") while a file's own tag usually just
    # has "X". That's a real, common pattern for this kind of tagging, and
    # without checking it separately it scores the same as an unrelated
    # long title happening to start with a short common word — exactly
    # the ambiguity the length-coverage rule above exists to reject. So
    # it's checked explicitly instead of by loosening that rule generally,
    # which would let the original false-positive back in.
    for text, other in ((left, right), (right, left)):
        if "/" in text:
            first_part = text.split("/", 1)[0].strip()
            if first_part and first_part.lower() != text.lower():
                best = max(best, _similarity_core(first_part, other))
    return best


def gated_score(*components: tuple[float, float, float]) -> float:
    """Combines (similarity, weight, floor) components into a weighted
    score — but only if EVERY component clears its own floor first.

    This exists because a plain weighted average lets a near-perfect
    match on one axis paper over a bad match on another. That's a real
    problem specifically for artist name, which is weighted highest but
    carries almost no discriminating power for a library that's mostly
    one artist: every candidate scores ~1.0 on it, which alone was
    enough to clear the old 0.72 threshold even when the album-title
    component was a weak, essentially-coincidental word overlap. Gating
    each axis independently means a wrong album title can no longer be
    rescued by a correct artist name, or vice versa.
    """
    total = 0.0
    for sim, weight, floor in components:
        if sim < floor:
            return 0.0
        total += sim * weight
    return total


# ---------------------------------------------------------------- cover art


@dataclass
class Artwork:
    data: bytes
    mime: str
    source: str
    url: str = ""

    @property
    def kilobytes(self) -> int:
        return len(self.data) // 1024


_art_cache: Dict[str, Optional[Artwork]] = {}
_art_lock = threading.Lock()


def _cache_get(key: str):
    with _art_lock:
        return _art_cache.get(key, "miss")


def _cache_put(key: str, value: Optional[Artwork]) -> None:
    with _art_lock:
        if len(_art_cache) >= ART_CACHE_LIMIT:
            _art_cache.pop(next(iter(_art_cache)))
        _art_cache[key] = value


def _download_image(url: str, source: str) -> Optional[Artwork]:
    body = _request(url, accept="image/*")
    if not body or len(body) < MIN_ART_BYTES or len(body) > MAX_ART_BYTES:
        return None
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif body[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    else:
        return None  # not an image we should be embedding
    return Artwork(data=body, mime=mime, source=source, url=url)


def _itunes_art(artist: str, album: str, size: int) -> Optional[Artwork]:
    query = urllib.parse.urlencode({
        "term": f"{artist} {album}".strip(), "entity": "album", "limit": 5})
    data = _get_json(f"https://itunes.apple.com/search?{query}")
    if not isinstance(data, dict):
        return None
    best = None
    best_score = 0.0
    for result in data.get("results", []):
        # Artist gets at least as much weight as album on purpose: album
        # titles are often short, common words ("Silence", "Echoes",
        # "Nights") that fuzzy-match plenty of wrong records, while an
        # artist name mismatch is the clearest signal something's wrong.
        # Both axes are gated independently (see gated_score) so a
        # near-perfect artist match — which is nearly guaranteed for a
        # library that's mostly one artist — can't rescue a wrong album.
        score = gated_score(
            (similarity(artist, result.get("artistName", "")), 0.55, 0.5),
            (similarity(album, result.get("collectionName", "")), 0.45, 0.5),
        )
        if score > best_score:
            best, best_score = result, score
    # Cover art is far more visible/embarrassing than a wrong genre tag if
    # it's wrong, so this threshold is deliberately stricter than the 0.6-0.62
    # used for metadata and lyrics matching.
    if not best or best_score < 0.72:
        return None
    thumb = best.get("artworkUrl100") or ""
    if not thumb:
        return None
    # iTunes will render the same artwork at any size on request.
    for candidate in (f"{size}x{size}bb", "1000x1000bb", "600x600bb"):
        art = _download_image(thumb.replace("100x100bb", candidate), "iTunes")
        if art:
            return art
    return None


def _itunes_track_art(artist: str, title: str) -> Optional[Artwork]:
    """Track-level fallback, mirroring _deezer_track_art: for releases
    that don't exist as a clean album match (mixtapes, singles folded
    into a later compilation, etc.) but do exist as an individual track -
    "House of Balloons" is exactly this case, since it started life as a
    free mixtape rather than a commercially catalogued album."""
    if not title:
        return None
    query = urllib.parse.urlencode({
        "term": f"{artist} {title}".strip(), "entity": "song", "limit": 10})
    data = _get_json(f"https://itunes.apple.com/search?{query}")
    if not isinstance(data, dict):
        return None
    best, best_score = None, 0.0
    for result in data.get("results", []):
        score = gated_score(
            (similarity(artist, result.get("artistName", "")), 0.5, 0.5),
            (similarity(title, result.get("trackName", "")), 0.5, 0.5),
        )
        if score > best_score:
            best, best_score = result, score
    if not best or best_score < 0.72:
        return None
    thumb = best.get("artworkUrl100") or ""
    if not thumb:
        return None
    for candidate in ("1000x1000bb", "600x600bb"):
        art = _download_image(thumb.replace("100x100bb", candidate), "iTunes")
        if art:
            return art
    return None


def _deezer_art(artist: str, album: str) -> Optional[Artwork]:
    # Deezer's advanced query syntax returns nothing here; plain text works.
    query = urllib.parse.urlencode({"q": f"{artist} {album}".strip(), "limit": 5})
    data = _get_json(f"https://api.deezer.com/search/album?{query}")
    if not isinstance(data, dict):
        return None
    best, best_score = None, 0.0
    for result in data.get("data", []):
        score = gated_score(
            (similarity(artist, (result.get("artist") or {}).get("name", "")), 0.55, 0.5),
            (similarity(album, result.get("title", "")), 0.45, 0.5),
        )
        if score > best_score:
            best, best_score = result, score
    if not best or best_score < 0.72:
        return None
    for key in ("cover_xl", "cover_big", "cover_medium"):
        if best.get(key):
            art = _download_image(best[key], "Deezer")
            if art:
                return art
    return None


def _coverart_archive(artist: str, album: str) -> Optional[Artwork]:
    """MusicBrainz lookup, then the Cover Art Archive.

    Tried both ways round: a specific release often has no art uploaded while
    the release group (the album as a concept, across all its editions) does.
    """
    query = urllib.parse.urlencode({
        "query": f'artist:"{artist}" AND release:"{album}"', "fmt": "json",
        "limit": 3})
    data = _get_json(f"https://musicbrainz.org/ws/2/release/?{query}")
    if isinstance(data, dict):
        for release in data.get("releases", [])[:3]:
            mbid = release.get("id")
            if not mbid:
                continue
            art = _download_image(
                f"https://coverartarchive.org/release/{mbid}/front",
                "Cover Art Archive")
            if art:
                return art

    group_query = urllib.parse.urlencode({
        "query": f'artist:"{artist}" AND releasegroup:"{album}"', "fmt": "json",
        "limit": 3})
    groups = _get_json(f"https://musicbrainz.org/ws/2/release-group/?{group_query}")
    if isinstance(groups, dict):
        for group in groups.get("release-groups", [])[:3]:
            mbid = group.get("id")
            if not mbid:
                continue
            art = _download_image(
                f"https://coverartarchive.org/release-group/{mbid}/front",
                "Cover Art Archive")
            if art:
                return art
    return None


def _deezer_track_art(artist: str, title: str) -> Optional[Artwork]:
    """Last resort: find the song, then take its album's cover.

    Covers the case where the album name is missing or wrong but the artist
    and title are known — singles and loose files, mostly.
    """
    if not title:
        return None
    query = urllib.parse.urlencode({"q": f"{artist} {title}".strip(), "limit": 5})
    data = _get_json(f"https://api.deezer.com/search/track?{query}")
    if not isinstance(data, dict):
        return None
    best, best_score = None, 0.0
    for result in data.get("data", []):
        title_sim = similarity(title, result.get("title", ""))
        if title_sim < 0.5:
            continue
        if artist:
            artist_sim = similarity(artist, (result.get("artist") or {}).get("name", ""))
            if artist_sim < 0.5:
                continue
            score = title_sim * 0.5 + artist_sim * 0.5
        else:
            score = title_sim * 0.5 + 0.15
        if score > best_score:
            best, best_score = result, score
    if not best or best_score < 0.72:
        return None
    album = best.get("album") or {}
    for key in ("cover_xl", "cover_big", "cover_medium"):
        if album.get(key):
            art = _download_image(album[key], "Deezer")
            if art:
                return art
    return None


def find_cover_art(artist: str, album: str, size: int = 1000,
                   title: str = "") -> Optional[Artwork]:
    """Look for front cover artwork, trying each source in turn.

    Cached per album, so a twelve-track album costs one lookup rather than
    twelve. Sources are ordered by image quality and hit rate: iTunes first
    (largest images), then Deezer, then the Cover Art Archive for older or
    more obscure releases, and finally a track-level search for when the
    album name is unknown.
    """
    artist, album = (artist or "").strip(), (album or "").strip()
    title = (title or "").strip()
    if not album and not artist and not title:
        return None
    key = f"{simplify(artist)}|{simplify(album)}|{simplify(title) if not album else ''}|{size}"
    cached = _cache_get(key)
    if cached != "miss":
        return cached  # type: ignore[return-value]

    lookups = []
    if album:
        lookups += [lambda: _itunes_art(artist, album, size),
                    lambda: _deezer_art(artist, album),
                    lambda: _coverart_archive(artist, album)]
    if title:
        # Track-level fallback for releases that don't exist as a clean
        # album match - mixtapes (House of Balloons started life as a free
        # mixtape, not a catalogued album), tracks folded into a later
        # compilation under a different collection name, etc.
        lookups += [lambda: _itunes_track_art(artist, title),
                    lambda: _deezer_track_art(artist, title)]

    art = None
    for lookup in lookups:
        if not network.available:
            break
        try:
            art = lookup()
        except Exception:
            art = None
        if art:
            break
    _cache_put(key, art)
    return art


# ------------------------------------------------------------------ lyrics


def _pick_lyrics(candidates: List[dict], title: str, artist: str,
                 duration: float) -> Optional[str]:
    best, best_score = None, 0.0
    for item in candidates:
        if item.get("instrumental"):
            continue
        synced = item.get("syncedLyrics")
        if not synced:
            continue
        score = (similarity(title, item.get("trackName", "")) * 0.6
                 + similarity(artist, item.get("artistName", "")) * 0.4)
        if duration:
            try:
                gap = abs(float(item.get("duration") or 0) - duration)
            except (TypeError, ValueError):
                gap = 999
            if gap > 8:
                # Almost certainly a different edit or a live version, and
                # timestamps from the wrong version are worse than none.
                continue
            score += max(0.0, (8 - gap) / 8) * 0.25
        if score > best_score:
            best, best_score = synced, score
    return best if best_score >= 0.6 else None


def find_synced_lyrics(artist: str, title: str, album: str = "",
                       duration: float = 0.0) -> Optional[str]:
    """Fetch timestamped .lrc lyrics from LRCLIB, or None."""
    if not artist or not title:
        return None
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(int(round(duration)))
    exact = _get_json("https://lrclib.net/api/get?" + urllib.parse.urlencode(params))
    if isinstance(exact, dict):
        chosen = _pick_lyrics([exact], title, artist, duration)
        if chosen:
            return chosen

    # Then a search by artist and title, and if that finds nothing, the same
    # search with the title tidied up — "(2011 Remaster)" and similar suffixes
    # are exactly the sort of thing that stops an otherwise perfect match.
    attempts = [title]
    tidy = simplify(title)
    if tidy and tidy != title.lower():
        attempts.append(tidy)

    for attempt in attempts:
        found = _get_json("https://lrclib.net/api/search?" + urllib.parse.urlencode(
            {"artist_name": artist, "track_name": attempt}))
        if isinstance(found, list) and found:
            chosen = _pick_lyrics(found[:20], title, artist, duration)
            if chosen:
                return chosen
    return None


# ---------------------------------------------------------------- metadata


@dataclass
class TrackFacts:
    fields: Dict[str, str] = field(default_factory=dict)
    source: str = ""
    confidence: float = 0.0


def find_track_metadata(artist: str, title: str, album: str = "",
                        duration: float = 0.0) -> Optional[TrackFacts]:
    """Look up per-track details (album, track number, year, genre).

    Needs at least an artist and a title to stand any chance - which is
    why filename inference runs first and this fills the remaining gaps.
    """
    if not title:
        return None
    terms = " ".join(part for part in (artist, title) if part)
    query = urllib.parse.urlencode({"term": terms, "entity": "song", "limit": 10})
    data = _get_json(f"https://itunes.apple.com/search?{query}")
    if not isinstance(data, dict):
        return None

    best, best_score = None, 0.0
    for result in data.get("results", []):
        title_sim = similarity(title, result.get("trackName", ""))
        if title_sim < 0.5:
            continue
        score = title_sim * 0.55
        if artist:
            artist_sim = similarity(artist, result.get("artistName", ""))
            if artist_sim < 0.5:
                continue
            score += artist_sim * 0.3
        else:
            score += 0.15
        if album:
            score += similarity(album, result.get("collectionName", "")) * 0.15
        if duration:
            millis = result.get("trackTimeMillis") or 0
            if millis:
                gap = abs(millis / 1000.0 - duration)
                if gap <= 5:
                    score += 0.1
                elif gap > 20:
                    score -= 0.2
        if score > best_score:
            best, best_score = result, score

    if not best or best_score < 0.62:
        return None

    facts: Dict[str, str] = {}
    if best.get("trackName"):
        facts["title"] = str(best["trackName"])
    if best.get("artistName"):
        facts["artist"] = str(best["artistName"])
    if best.get("collectionArtistName") or best.get("artistName"):
        facts["albumartist"] = str(best.get("collectionArtistName")
                                   or best["artistName"])
    if best.get("collectionName"):
        facts["album"] = str(best["collectionName"])
    if best.get("trackNumber"):
        facts["tracknumber"] = str(best["trackNumber"])
    if best.get("trackCount"):
        facts["tracktotal"] = str(best["trackCount"])
    if best.get("discNumber"):
        facts["discnumber"] = str(best["discNumber"])
    if best.get("releaseDate"):
        facts["date"] = str(best["releaseDate"])[:4]
    if best.get("primaryGenreName"):
        facts["genre"] = str(best["primaryGenreName"])

    return TrackFacts(fields=facts, source="iTunes",
                      confidence=round(min(best_score, 1.0), 3))


def status() -> Dict[str, Any]:
    return {
        "online": network.available,
        "reason": network.reason,
        "art_cached": len(_art_cache),
    }


# ---------------------------------------------------------------- copyright

_copyright_cache: Dict[str, Optional[str]] = {}
_copyright_lock = threading.Lock()


def find_copyright(artist: str, album: str) -> Optional[str]:
    """Looks up a phonogram copyright line from MusicBrainz's release/label
    data, formatted the conventional way: "(P) {year} {label}". There's
    no second source for this one - iTunes and Deezer's public APIs don't
    expose it - so a release MusicBrainz doesn't know the label for (common
    for smaller or self-released records) just comes back as None."""
    artist, album = (artist or "").strip(), (album or "").strip()
    if not artist or not album:
        return None
    key = f"{simplify(artist)}|{simplify(album)}"
    with _copyright_lock:
        if key in _copyright_cache:
            return _copyright_cache[key]

    result = None
    query = urllib.parse.urlencode({
        "query": f'artist:"{artist}" AND release:"{album}"',
        "fmt": "json", "limit": 3, "inc": "labels"})
    data = _get_json(f"https://musicbrainz.org/ws/2/release/?{query}")
    if isinstance(data, dict):
        for release in data.get("releases", [])[:3]:
            label_name = ""
            for info in release.get("label-info") or []:
                label = info.get("label") or {}
                if label.get("name"):
                    label_name = label["name"]
                    break
            if not label_name:
                continue
            year = (release.get("date") or "")[:4]
            result = f"\u2117 {year} {label_name}" if year.isdigit() else f"\u2117 {label_name}"
            break

    with _copyright_lock:
        _copyright_cache[key] = result
    return result
