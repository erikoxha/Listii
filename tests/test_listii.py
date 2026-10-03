import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import pytest
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, TRCK, APIC
from listii.models import Track, Source, Entry, Playlist, playlist_id
from listii.sources import match, assign_sources, load_manifest
from listii.spotify import Spotify
from listii.metadata import embed
from listii.export import Exporter, Options, filename, safe_name, Cancelled


def track(position=1, **kwargs):
    values = dict(position=position, title="Song", artists=["Artist"], album="Album", duration_ms=1000,
                  isrc="USAAA0000001", album_track=7, disc=2, year="2024", album_artists=["Album Artist"])
    values.update(kwargs)
    return Track(**values)


def source(location="audio.mp3", **kwargs):
    values = dict(location=location, title="Song", artists=["Artist"], album="Album", duration_ms=1000,
                  isrc="USAAA0000001", permission="User-provided file")
    values.update(kwargs)
    return Source(**values)

@pytest.mark.parametrize("url", ["https://open.spotify.com/playlist/" + "a" * 22 + "?si=hello", "spotify:playlist:" + "a" * 22, "https://open.spotify.com/intl-de/playlist/" + "a" * 22])
def test_valid_links(url):
    assert playlist_id(url) == "a" * 22

@pytest.mark.parametrize("url", ["https://evil.example/playlist/" + "a" * 22, "https://open.spotify.com/track/" + "a" * 22, "http://open.spotify.com/playlist/" + "a" * 22, "https://open.spotify.com/playlist/abc"])
def test_invalid_links(url):
    with pytest.raises(ValueError):
        playlist_id(url)


def test_all_pages_duplicates_and_removed_entries():
    spotify = object.__new__(Spotify)
    raw = {"name": "Song", "id": "duplicate", "type": "track", "artists": [{"name": "Artist"}]}
    calls = []
    def get(path, cancel=None):
        calls.append(path)
        if path == "page2":
            return {"items": [{"item": raw}, {"item": None}], "next": None}
        if "items?" in path:
            return {"items": [{"item": raw}, {"track": raw}], "next": "page2"}
        return {"name": "Test", "snapshot_id": "stable"}
    spotify.get = get
    result = spotify.playlist("spotify:playlist:" + "a" * 22)
    assert [e.track.position for e in result.entries] == [1, 2, 3, 4]
    assert [e.track.spotify_id for e in result.entries[:3]] == ["duplicate"] * 3
    assert not result.entries[-1].track.supported
    assert "page2" in calls


def test_matching_rejects_versions_isrc_duration():
    assert match(track(), source())[1]
    assert match(track(), source(isrc="US-AAA-00-00001"))[1]
    for candidate in [source(title="Song (Live)"), source(isrc="GBZZZ0000002"), source(duration_ms=12000), source(title="Song (Remix)"), source(artists=["Cover Band"])]:
        score, certain, _ = match(track(), candidate)
        assert not certain
        assert score < 35


def test_missing_identifiers_and_ties_require_review():
    entries = [Entry(track()), Entry(track(2))]
    assign_sources(entries, [source(album="", isrc="")])
    assert [e.status for e in entries] == ["review", "review"]
    assign_sources(entries, [source(), source("other.mp3")])
    assert all(e.status == "review" for e in entries)
    assign_sources(entries, [source()])
    assert all(e.status == "ready" for e in entries)
    assert entries[0].source is entries[1].source


def test_manifest_requires_permission_and_https(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps([{"location": "https://example.com/song.flac", "title": "Song", "artists": ["Artist"], "permission": ""}]))
    with pytest.raises(ValueError):
        load_manifest(path)
    path.write_text(json.dumps([{"location": "http://example.com/song.flac", "title": "Song", "artists": ["Artist"], "permission": "CC0"}]))
    with pytest.raises(ValueError):
        load_manifest(path)


def test_filename_order_sanitization_and_album_track():
    assert filename(track(1), 12, "{position} - {artist} - {title}") == "001 - Artist - Song.mp3"
    assert filename(track(2), 12, "{title}").startswith("002 - ")
    assert safe_name("CON") == "_CON"
    assert "/" not in safe_name("a/b\n")
    assert track().album_track == 7
    with pytest.raises(ValueError):
        filename(track(), 1, "{invalid}")

@pytest.fixture
def audio(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg integration test requires ffmpeg")
    path = tmp_path / "source.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame", str(path)], check=True)
    tags = ID3(path)
    tags.add(TIT2(encoding=3, text=["Source title"]))
    tags.add(TPE1(encoding=3, text=["Source artist"]))
    tags.add(TALB(encoding=3, text=["Source album"]))
    tags.add(TCON(encoding=3, text=["Ambient"]))
    tags.add(TRCK(encoding=3, text=["9/12"]))
    tags.add(APIC(encoding=3, mime="image/png", type=3, data=b"source-cover"))
    tags.save(path)
    return path


def test_metadata_album_number_source_fallback_artwork(audio, tmp_path):
    output = tmp_path / "converted.mp3"
    shutil.copyfile(audio, output)
    embed(output, track(), audio, (b"spotify-cover", "image/jpeg"))
    tags = ID3(output)
    assert str(tags["TIT2"]) == "Song"
    assert str(tags["TRCK"]) == "7"
    assert str(tags["TPOS"]) == "2"
    assert str(tags["TPE2"]) == "Album Artist"
    assert str(tags["TCON"]) == "Ambient"
    assert tags.getall("APIC")[0].data == b"spotify-cover"
    embed(output, track(album="", year="", album_track=0), audio)
    tags = ID3(output)
    assert str(tags["TALB"]) == "Source album"
    assert str(tags["TRCK"]) == "9/12"
    assert tags.getall("APIC")[0].data == b"source-cover"


def playlist(audio, count=3):
    return Playlist("test", "Playlist", [Entry(track(i), source(str(audio)), "ready") for i in range(1, count + 1)])


def test_real_exports_duplicates_order_and_recovery(audio, tmp_path):
    options = Options(str(tmp_path / "exports"), subfolder=False)
    exporter = Exporter(playlist(audio), options)
    report, results = exporter.run()
    assert [r["position"] for r in results] == [1, 2, 3]
    assert all(r["status"] == "successful" for r in results)
    files = sorted((tmp_path / "exports").glob("*.mp3"))
    assert len(files) == 3
    assert all(str(ID3(path)["TRCK"]) == "7" for path in files)
    lines = (tmp_path / "exports" / "Playlist.m3u8").read_text().splitlines()
    assert [line for line in lines if not line.startswith("#")] == [p.name for p in files]
    resumed = Exporter(playlist(audio), options)
    resumed.convert = lambda *args: pytest.fail("Verified exports must not be converted again")
    _, results = resumed.run()
    assert all(r["status"] == "successful" for r in results)
    assert all("Recovered" in r["detail"] for r in results)


def test_cancel_then_retry_retains_correct_positions(audio, tmp_path):
    cancel = threading.Event()
    options = Options(str(tmp_path / "exports"), subfolder=False, workers=1)
    def progress(position, percent, status, detail):
        if position == 1 and status == "successful":
            cancel.set()
    _, results = Exporter(playlist(audio), options, progress, cancel).run()
    assert results[0]["status"] == "successful"
    assert results[1]["status"] == "cancelled"
    _, results = Exporter(playlist(audio), options).run()
    assert all(r["status"] == "successful" for r in results)
    assert len(list((tmp_path / "exports").glob("*.mp3"))) == 3


def test_existing_files_skip_rename_overwrite(audio, tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    existing = root / "001 - Artist - Song.mp3"
    existing.write_bytes(b"keep me")
    _, results = Exporter(playlist(audio, 1), Options(str(root), subfolder=False)).run()
    assert results[0]["status"] == "skipped" and existing.read_bytes() == b"keep me"
    _, results = Exporter(playlist(audio, 1), Options(str(root), subfolder=False, existing="rename")).run()
    assert results[0]["file"].endswith("(2).mp3") and existing.read_bytes() == b"keep me"
    _, results = Exporter(playlist(audio, 1), Options(str(root), subfolder=False, existing="overwrite")).run()
    assert results[0]["status"] == "successful" and existing.read_bytes() != b"keep me"


def test_unavailable_and_review_sources_never_convert(tmp_path):
    entries = [Entry(track()), Entry(track(2), source(), "review")]
    exporter = Exporter(Playlist("test", "Test", entries), Options(str(tmp_path), subfolder=False))
    exporter.convert = lambda *args: pytest.fail("Unavailable audio must not convert")
    _, results = exporter.run()
    assert all(r["status"] == "unavailable" for r in results)


def test_http_interrupt_resume_with_validator(tmp_path, monkeypatch):
    from listii import export
    s = source("https://licensed.example/song.flac", permission="CC0")
    exporter = Exporter(Playlist("p", "P", [Entry(track(), s, "ready")]), Options(str(tmp_path), subfolder=False))
    headers_seen = []
    class Response:
        status_code = 200
        url = s.location
        headers = {"ETag": '"recording-v1"', "Content-Length": "6"}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def raise_for_status(self):
            pass
        def iter_content(self, size):
            yield b"abc"
            exporter.cancel.set()
            yield b"def"
    monkeypatch.setattr(export.requests, "get", lambda *args, **kwargs: Response())
    with pytest.raises(Cancelled):
        exporter.download(s, 1)
    assert (exporter.cache / "1.audio").read_bytes() == b"abc"
    exporter.cancel.clear()
    class Resumed(Response):
        status_code = 206
        headers = {"ETag": '"recording-v1"', "Content-Length": "3", "Content-Range": "bytes 3-5/6"}
        def iter_content(self, size):
            yield b"def"
    def resumed_get(*args, **kwargs):
        headers_seen.append(kwargs["headers"])
        return Resumed()
    monkeypatch.setattr(export.requests, "get", resumed_get)
    assert exporter.download(s, 1).read_bytes() == b"abcdef"
    assert headers_seen[0]["Range"] == "bytes=3-"
    assert headers_seen[0]["If-Range"] == '"recording-v1"'
    monkeypatch.setattr(export.requests, "get", lambda *args, **kwargs: pytest.fail("Complete transfer must use cache"))
    assert exporter.download(s, 1).read_bytes() == b"abcdef"


def test_http_changed_source_restarts_instead_of_appending(tmp_path, monkeypatch):
    from listii import export
    s = source("https://licensed.example/song.flac", permission="CC0")
    exporter = Exporter(Playlist("p", "P", []), Options(str(tmp_path), subfolder=False))
    (exporter.cache / "1.audio").write_bytes(b"old")
    (exporter.cache / "1.http.json").write_text(json.dumps({"validator": '"old"'}))
    class Response:
        status_code = 200
        url = s.location
        headers = {"ETag": '"new"', "Content-Length": "3"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield b"new"
    monkeypatch.setattr(export.requests, "get", lambda *args, **kwargs: Response())
    assert exporter.download(s, 1).read_bytes() == b"new"


def test_spotify_refresh_and_rate_limit(monkeypatch):
    import time
    from listii import spotify as module
    client = object.__new__(Spotify)
    client.token = {"access_token": "expired", "expires_at": time.time() + 999}
    client.lock = threading.RLock()
    refreshed = []
    client.refresh = lambda: (refreshed.append(True), client.token.update(access_token="fresh"))
    class Response:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"Retry-After": "1"}
            self.ok = status == 200
        def json(self): return {"ok": True}
    responses = iter([Response(401), Response(429), Response(200)])
    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert client.get("playlists/test") == {"ok": True}
    assert refreshed == [True]


def test_stale_lock_recovers_after_process_crash(audio, tmp_path):
    exporter = Exporter(playlist(audio, 1), Options(str(tmp_path), subfolder=False))
    lock = exporter.cache / "active.lock"
    lock.mkdir()
    (lock / "pid").write_text("99999999")
    _, results = exporter.run()
    assert results[0]["status"] == "successful"
    assert not lock.exists()
