import hashlib
import json
import os
import re
import shutil
import uuid
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse
import requests
from .metadata import embed

class Cancelled(Exception):
    pass

@dataclass
class Options:
    destination: str
    quality: int = 320
    pattern: str = "{position} - {artist} - {title}"
    subfolder: bool = True
    existing: str = "skip"
    workers: int = 3


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", value).strip(" .")
    value = value[:150].rstrip(" .") or "Untitled"
    while len(value.encode("utf-8")) > 220:
        value = value[:-1]
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *["COM%d" % i for i in range(1, 10)], *["LPT%d" % i for i in range(1, 10)]}:
        value = "_" + value
    return value


def filename(track, count, pattern):
    width = max(3, len(str(count)))
    position = str(track.position).zfill(width)
    try:
        name = pattern.format(position=position, title=track.title,
                              artist=", ".join(track.artists) or "Unknown artist", album=track.album)
    except (KeyError, ValueError, IndexError, AttributeError) as exc:
        raise ValueError("Use only {position}, {artist}, {title}, and {album} in the filename format.") from exc
    # A mandatory positional prefix makes duplicates unique and ordered even in custom formats.
    name = safe_name(name)
    if not name.startswith(position + " ") and name != position:
        name = position + " - " + name
    return name + ".mp3"


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


class Exporter:
    def __init__(self, playlist, options, progress=None, cancel=None):
        self.playlist, self.options = playlist, options
        if options.quality not in (128, 192, 256, 320) or options.existing not in ("skip", "rename", "overwrite"):
            raise ValueError("Invalid export options")
        filename(playlist.entries[0].track, len(playlist.entries), options.pattern) if playlist.entries else None
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda *args: None)
        self.lock = threading.Lock()
        self.root = Path(options.destination).expanduser().resolve()
        if options.subfolder:
            self.root /= safe_name(playlist.name)
        signature = {"id": playlist.id, "snapshot": playlist.snapshot,
                     "entries": [{"track": asdict(e.track), "source": asdict(e.source) if e.source else None} for e in playlist.entries],
                     "quality": options.quality, "pattern": options.pattern, "policy": options.existing}
        self.job = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
        self.cache = self.root / ".listii" / self.job
        self.cache.mkdir(parents=True, exist_ok=True)
        self.state_path = self.cache / "state.json"
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.results = {}

    def check(self):
        if self.cancel.is_set():
            raise Cancelled("Cancelled; retry this export to resume.")

    def emit(self, position, percent, status, detail):
        self.progress(position, percent, status, detail)

    def download(self, source, position):
        if not source.location.startswith("https://"):
            path = Path(source.location)
            if not path.is_file():
                raise ValueError("Source file is missing. Reattach it and retry.")
            return path
        if not source.permission:
            raise ValueError("The source does not declare download permission.")
        path = self.cache / (str(position) + ".audio")
        meta_path = self.cache / (str(position) + ".http.json")
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            meta = {}
        if meta.get("complete") and path.exists() and digest(path) == meta.get("sha256"):
            return path
        for attempt in range(4):
            self.check()
            size = path.stat().st_size if path.exists() else 0
            validator = meta.get("validator")
            headers = {"Accept-Encoding": "identity"}
            if size and validator:
                headers.update({"Range": "bytes=%d-" % size, "If-Range": validator})
            try:
                with requests.get(source.location, headers=headers, stream=True, timeout=(10, 30)) as response:
                    if urlparse(response.url).scheme != "https":
                        raise ValueError("Audio source redirected to an insecure address.")
                    if response.status_code == 429 or response.status_code >= 500:
                        delay = min(60, max(1, float(response.headers.get("Retry-After", 2 ** attempt))))
                        if self.cancel.wait(delay):
                            self.check()
                        continue
                    # Restart if server cannot resume, or the remote recording changed.
                    if response.status_code == 416:
                        path.unlink(missing_ok=True)
                        meta = {}
                        continue
                    response.raise_for_status()
                    append = bool(size and validator and response.status_code == 206)
                    incoming_validator = response.headers.get("ETag") or response.headers.get("Last-Modified")
                    if append and incoming_validator and incoming_validator != validator:
                        path.unlink(missing_ok=True)
                        meta = {}
                        continue
                    if append and not response.headers.get("Content-Range", "").startswith("bytes %d-" % size):
                        raise ValueError("Source returned an invalid resume range.")
                    if not append:
                        size = 0
                    total = size + int(response.headers.get("Content-Length", 0))
                    meta = {"validator": response.headers.get("ETag") or response.headers.get("Last-Modified"), "complete": False}
                    atomic_json(meta_path, meta)
                    with path.open("ab" if append else "wb") as stream:
                        for chunk in response.iter_content(128 * 1024):
                            self.check()
                            if chunk:
                                stream.write(chunk)
                                size += len(chunk)
                                self.emit(position, min(65, int(size / total * 65)) if total else 10,
                                          "downloading", "%s received" % format_bytes(size))
                    if total and size != total:
                        raise requests.ConnectionError("Incomplete response")
                    meta.update(complete=True, sha256=digest(path))
                    atomic_json(meta_path, meta)
                    return path
            except requests.RequestException:
                if attempt == 3:
                    raise RuntimeError("Audio transfer failed. Retry to resume if the server supports ranges.")
                if self.cancel.wait(2 ** attempt):
                    self.check()
        raise RuntimeError("Source is rate limited or temporarily unavailable. Retry later.")

    def artwork(self, url):
        if not url:
            return None
        try:
            if urlparse(url).scheme != "https":
                return None
            with requests.get(url, timeout=15, stream=True) as response:
                response.raise_for_status()
                mime = response.headers.get("Content-Type", "").split(";")[0]
                if mime not in ("image/jpeg", "image/png"):
                    return None
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    self.check()
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks), mime
        except requests.RequestException:
            return None

    def convert(self, source, target, position):
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("FFmpeg is required. Install it and restart Listii.")
        log = self.cache / (str(position) + ".ffmpeg.log")
        with log.open("wb") as output:
            process = subprocess.Popen([executable, "-nostdin", "-y", "-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:a:0",
                                        "-vn", "-map_metadata", "-1", "-codec:a", "libmp3lame", "-b:a",
                                        str(self.options.quality) + "k", str(target)], stdout=subprocess.DEVNULL, stderr=output)
            try:
                while process.poll() is None:
                    self.check()
                    self.cancel.wait(0.15)
            except Cancelled:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            if process.returncode:
                raise RuntimeError("FFmpeg could not decode this audio file. See the job's ffmpeg log.")

    def commit(self, temp, target):
        if self.options.existing == "overwrite":
            os.replace(temp, target)
            return target
        candidate = target
        for suffix in range(10000):
            try:
                # Atomic no-clobber publish; temp and destination are on the same filesystem.
                os.link(temp, candidate)
                temp.unlink()
                return candidate
            except FileExistsError:
                if self.options.existing == "skip":
                    return None
                candidate = target.with_name(target.stem + " (%d)" % (suffix + 2) + target.suffix)
        raise RuntimeError("Too many existing files with this name.")

    def one(self, entry):
        track, position = entry.track, entry.track.position
        target = self.root / filename(track, len(self.playlist.entries), self.options.pattern)
        saved = self.state.get(str(position), {})
        restored = self.root / (saved.get("file") or "")
        if saved.get("status") == "successful" and restored.is_file() and digest(restored) == saved.get("sha256"):
            return self.finish(entry, "successful", "Recovered verified completed MP3", restored)
        if not entry.source or entry.status == "review" and not entry.approved:
            return self.finish(entry, "unavailable", entry.detail)
        if target.exists() and self.options.existing == "skip":
            return self.finish(entry, "skipped", "Existing file kept; not verified as this recording")
        temp = self.cache / (str(position) + ".mp3")
        try:
            self.check()
            self.emit(position, 0, "starting", "Preparing audio source")
            source = self.download(entry.source, position)
            self.check()
            self.emit(position, 70, "converting", "Encoding MP3 at %d kbps" % self.options.quality)
            self.convert(source, temp, position)
            self.check()
            self.emit(position, 90, "tagging", "Embedding original album metadata")
            art = self.artwork(track.artwork)
            embed(temp, track, source, art)
            self.check()
            result = self.commit(temp, target)
            if result is None:
                return self.finish(entry, "skipped", "Existing file kept")
            warning = "Saved" if art or not track.artwork else "Saved; Spotify artwork unavailable, source artwork retained if present"
            return self.finish(entry, "successful", warning, result)
        except Cancelled as exc:
            return self.finish(entry, "cancelled", str(exc))
        except Exception as exc:
            return self.finish(entry, "failed", str(exc))
        finally:
            temp.unlink(missing_ok=True)

    def finish(self, entry, status, detail, path=None):
        result = {"position": entry.track.position, "title": entry.track.title, "artists": entry.track.artists,
                  "status": status, "detail": detail, "file": path.name if path else None}
        if path:
            result["sha256"] = digest(path)
        with self.lock:
            self.state[str(entry.track.position)] = result
            atomic_json(self.state_path, self.state)
            self.results[entry.track.position] = result
        self.emit(entry.track.position, 100 if status != "cancelled" else 0, status, detail)
        return result

    def run(self):
        # Prevent two app instances from racing on the same job state.
        lock_path = self.cache / "active.lock"
        try:
            lock_path.mkdir(exist_ok=False)
        except FileExistsError:
            try:
                owner = int((lock_path / "pid").read_text())
                os.kill(owner, 0)
            except ProcessLookupError:
                (lock_path / "pid").unlink(missing_ok=True)
                lock_path.rmdir()
                lock_path.mkdir(exist_ok=False)
            except (ValueError, FileNotFoundError):
                raise RuntimeError("A job lock is incomplete. Close other Listii instances, then remove the job's active.lock folder.")
            else:
                raise RuntimeError("This export is already running in another Listii instance.")
        (lock_path / "pid").write_text(str(os.getpid()))
        try:
            with ThreadPoolExecutor(max_workers=max(1, min(6, self.options.workers))) as pool:
                futures = [pool.submit(self.one, entry) for entry in self.playlist.entries]
                for future in as_completed(futures):
                    future.result()
            ordered = [self.results[p] for p in sorted(self.results)]
            stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            report = self.root / ("Listii report " + stamp + ".json")
            atomic_json(report, {"playlist": self.playlist.name, "job": self.job, "quality_kbps": self.options.quality,
                                 "counts": {s: sum(r["status"] == s for r in ordered) for s in ("successful", "skipped", "unavailable", "failed", "cancelled")}, "tracks": ordered})
            # Reports and M3Us never replace existing user files.
            lines = ["#EXTM3U"]
            for result in ordered:
                if result["status"] == "successful":
                    track = self.playlist.entries[result["position"] - 1].track
                    title = (", ".join(track.artists) + " - " + track.title).replace("\n", " ").replace("\r", " ")
                    lines += ["#EXTINF:%d,%s" % (track.duration_ms // 1000, title), result["file"]]
            m3u = self.root / (safe_name(self.playlist.name) + ".m3u8")
            for suffix in range(10000):
                try:
                    with m3u.open("x", encoding="utf-8") as stream:
                        stream.write("\n".join(lines) + "\n")
                    break
                except FileExistsError:
                    m3u = self.root / (safe_name(self.playlist.name) + " (%d).m3u8" % (suffix + 2))
            return report, ordered
        finally:
            (lock_path / "pid").unlink(missing_ok=True)
            lock_path.rmdir()


def format_bytes(size):
    return "%.1f MB" % (size / 1024 / 1024)
