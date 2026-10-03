import json
import re
from pathlib import Path
from urllib.parse import urlparse
import mutagen
from .models import Source
from .metadata import source_metadata

AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".aiff"}

def read_source(path):
    media = mutagen.File(path, easy=True)
    tags, _ = source_metadata(path)
    def first(key):
        values = tags.get(key, [])
        return str(values[0]) if values else ""
    return Source(str(Path(path).resolve()), first("title"), list(tags.get("artist", [])),
                  first("album"), int(media.info.length * 1000) if media and media.info else 0,
                  first("isrc"), "User-provided file")

def scan_files(paths):
    sources, errors = [], []
    for path in paths:
        try:
            sources.append(read_source(path))
        except Exception as exc:
            errors.append(Path(path).name + ": " + str(exc))
    return sources, errors

def load_manifest(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Manifest must be a JSON array of audio sources.")
    result = []
    for row in data:
        source = Source(**row)
        uri = urlparse(source.location)
        if uri.scheme != "https" or not uri.hostname or uri.username or uri.password:
            raise ValueError("Manifest sources must use HTTPS without credentials in the URL.")
        if not source.permission.strip():
            raise ValueError("Each source needs a permission statement or license reference.")
        if not source.title or not source.artists:
            raise ValueError("Each source needs a title and artists for matching.")
        result.append(source)
    return result

def normalize(value):
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()

def identifier(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())

def versions(value):
    words = normalize(value).split()
    return {w for w in words if w in {"live", "remix", "remixed", "cover", "acoustic", "instrumental", "karaoke", "demo", "edit", "extended", "remaster", "remastered"}}

def match(track, source):
    if not track.supported:
        return 0, False, "Unsupported playlist entry"
    if versions(track.title) != versions(source.title + " " + source.version):
        return 0, False, "Recording version differs"
    if track.isrc and source.isrc and identifier(track.isrc) != identifier(source.isrc):
        return 0, False, "ISRC differs"
    same_isrc = bool(track.isrc and source.isrc and identifier(track.isrc) == identifier(source.isrc))
    same_title = normalize(track.title) == normalize(source.title)
    same_artist = bool(set(map(normalize, track.artists)) & set(map(normalize, source.artists)))
    if track.artists and source.artists and not same_artist:
        return 0, False, "Credited artists differ"
    duration_known = bool(track.duration_ms and source.duration_ms)
    duration_ok = duration_known and abs(track.duration_ms - source.duration_ms) <= 2000
    if duration_known and abs(track.duration_ms - source.duration_ms) > 5000:
        return 0, False, "Duration differs by more than five seconds"
    same_album = bool(track.album and source.album and normalize(track.album) == normalize(source.album))
    score = int(same_isrc) * 60 + int(same_title) * 20 + int(same_artist) * 15 + int(same_album) * 8 + int(duration_ok) * 7
    confident = same_title and same_artist and duration_ok and (same_isrc or same_album)
    return score, confident, "Identifiers, recording version and duration agree" if confident else "Missing or uncertain identifiers; review this recording"

def assign_sources(entries, sources):
    for entry in entries:
        entry.source, entry.approved = None, False
        ranked = sorted([(match(entry.track, s), s) for s in sources], key=lambda x: x[0][0], reverse=True)
        if not ranked or ranked[0][0][0] < 35:
            entry.status = "unavailable"
            entry.detail = "No permitted audio source matches this recording."
            continue
        (score, confident, reason), source = ranked[0]
        if len(ranked) > 1 and ranked[1][0][0] == score:
            confident, reason = False, "Multiple equally strong sources; review the selected recording"
        entry.source = source
        entry.status = "ready" if confident else "review"
        entry.detail = reason
