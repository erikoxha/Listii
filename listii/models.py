from dataclasses import dataclass, field, asdict
from typing import List, Optional
import re
from urllib.parse import urlparse

@dataclass
class Track:
    position: int
    title: str
    artists: List[str] = field(default_factory=list)
    album: str = ""
    album_artists: List[str] = field(default_factory=list)
    duration_ms: int = 0
    isrc: str = ""
    year: str = ""
    disc: int = 0
    album_track: int = 0
    artwork: str = ""
    spotify_id: str = ""
    supported: bool = True

@dataclass
class Source:
    location: str
    title: str = ""
    artists: List[str] = field(default_factory=list)
    album: str = ""
    duration_ms: int = 0
    isrc: str = ""
    permission: str = ""
    version: str = ""

@dataclass
class Entry:
    track: Track
    source: Optional[Source] = None
    status: str = "unavailable"
    detail: str = "No permitted audio source. Add your files or an authorized manifest."
    approved: bool = False

@dataclass
class Playlist:
    id: str
    name: str
    entries: List[Entry]
    snapshot: str = ""


def playlist_id(value):
    value = value.strip()
    if value.startswith("spotify:playlist:"):
        candidate = value.split(":")[-1]
    else:
        url = urlparse(value)
        if url.scheme != "https" or url.hostname != "open.spotify.com":
            raise ValueError("Use an https://open.spotify.com/playlist/… link.")
        match = re.fullmatch(r"/(?:intl-[a-z]{2}/)?playlist/([A-Za-z0-9]{22})/?", url.path)
        if not match:
            raise ValueError("This is not a Spotify playlist link.")
        candidate = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9]{22}", candidate):
        raise ValueError("The playlist identifier must contain 22 letters or numbers.")
    return candidate


def parse_track(item, position):
    raw = item.get("item", item.get("track")) or {}
    album = raw.get("album") or {}
    images = album.get("images") or []
    return Track(position=position, title=raw.get("name") or "Unavailable entry",
                 artists=[a.get("name", "") for a in raw.get("artists", [])],
                 album=album.get("name", ""),
                 album_artists=[a.get("name", "") for a in album.get("artists", [])],
                 duration_ms=raw.get("duration_ms") or 0,
                 isrc=(raw.get("external_ids") or {}).get("isrc", ""),
                 year=album.get("release_date", "")[:4], disc=raw.get("disc_number") or 0,
                 album_track=raw.get("track_number") or 0,
                 artwork=images[0]["url"] if images else "", spotify_id=raw.get("id") or "",
                 supported=bool(raw) and raw.get("type", "track") == "track")
