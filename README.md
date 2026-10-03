# Listii

A native desktop app for turning a Spotify playlist into an ordered library of individual MP3s **using audio you provide or sources that permit downloading and conversion**. Spotify supplies playlist information, not full-track audio. No Spotify stream capture, DRM removal, scraping, preview-audio substitution, or fake downloads are implemented.

## Run

Requires Python 3.9+ (3.11+ with modern OpenSSL recommended) and FFmpeg (with the `libmp3lame` encoder).

```sh
python3 -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
listii
```

Install FFmpeg with your platform's package manager (`brew install ffmpeg` on macOS, `winget install Gyan.FFmpeg` on Windows, or your Linux distribution's ffmpeg package). Restart the app after installing. The executable must be on PATH; Listii never installs binaries automatically.

## Spotify setup

1. Create an app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard).
2. Register **exactly** `http://127.0.0.1:8765/callback` as its redirect URI. Enable Web API access.
3. Copy the **Client ID**. Do not supply a client secret; Listii uses [Authorization Code with PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow).
4. Click **Connect Spotify**, enter the Client ID, and authorize in your browser. Scopes: `playlist-read-private` and `playlist-read-collaborative`.
5. Paste a Spotify playlist URL and select **Load playlist**.

Access and refresh tokens live in the OS credential store via `keyring`, never a project config file. A working macOS Keychain, Windows Credential Locker, or Linux Secret Service is required; there is no plaintext fallback. Client IDs and the last destination are ordinary preferences. Sign-in binds only to 127.0.0.1 and verifies a random OAuth state. The browser flow times out after three minutes. To revoke access use Spotify's account app permissions; to remove stored tokens use the OS credential manager entry `Listii Spotify` for your Client ID.

**Spotify access limitations:** authentication does not guarantee every playlist is available through your developer app. Spotify's [2026 API changes](https://developer.spotify.com/documentation/web-api/references/changes/february-2026) restrict playlist contents in development mode to playlists owned by or collaborated on by the authenticated user. Development-mode account and app eligibility requirements also apply. Private playlists require both the requested scopes and access allowed by Spotify. Listii uses `/playlists/{id}/items`, accepts both `item` and legacy `track` payloads, fetches all pages, and retries if the playlist snapshot changes during import. A 403/404 is reported explicitly rather than worked around.

## Attach permitted audio

**Audio files:** select files you have permission to convert. Supported inputs include tagged MP3, FLAC, M4A, Ogg, Opus, WAV, AAC and AIFF, subject to your FFmpeg build. Automatic matching depends on embedded title, artist, album, duration, and ISRC. Untagged files cannot be matched automatically: add accurate tags using a tag editor first. Input files are read only.

**Authorized links:** import a JSON array using the schema below. URLs must be HTTPS direct audio downloads (not web pages, streaming services or playlists). Use public licensed downloads or a download URL issued by a service that grants you permission to download **and convert** the recording. Do not include secrets or account cookies. Signed URLs may expire; obtain a fresh manifest if necessary. Listii does not verify license claims or search the web for audio; the permission statement records your source's authorization.

```json
[
  {
    "location": "https://your-authorized-host.example/audio/recording.flac",
    "title": "Exact song title",
    "artists": ["Artist"],
    "album": "Original album",
    "duration_ms": 213420,
    "isrc": "USAAA2400001",
    "version": "",
    "permission": "License/source reference permitting this download and MP3 conversion"
  }
]
```

The example address is deliberately nonfunctional. The app has no sample playlist or mock download mode.

## Matching and export

- Every playlist entry gets its own position, including duplicate tracks and removed/unsupported items. Unsupported items remain unavailable.
- Automatic approval requires exact normalized title, an overlapping credited artist, duration within two seconds, and matching ISRC or album. Conflicting supplied ISRCs, version markers, or durations differing by over five seconds block matching. Missing identifiers and equally strong candidates require review. Live, remix, acoustic, cover, instrumental and other recognized version markers must agree. Matching is conservative, not an acoustic fingerprint or a guarantee of recording identity.
- Select a row and **Review match** to compare source location, duration, album, ISRC, version and permission. Only compatible candidates can be approved. A source can serve multiple duplicate playlist entries.
- Choose folder, bitrate (128/192/256/320 kbps), filename format and existing-file policy. Available fields: `{position}`, `{artist}`, `{title}`, `{album}`. A zero-padded positional prefix is always retained, independently of album track numbering. Forbidden filesystem characters and reserved Windows names are sanitized.
- Conversion cannot improve source quality. Lossy-to-lossy conversion can reduce it. Output is constant bitrate MP3 using FFmpeg, even if the input is already MP3.
- ID3v2.3 embeds title, artists, album, album artist, release year, disc number, **original album track number**, ISRC and original album artwork. Spotify does not normally provide genre; source genre and other supported source fields are retained where Spotify supplies no value. Missing Spotify artwork falls back to source cover art and is noted in the report. A playlist position never replaces `TRCK`.
- MP3s are committed atomically only after successful conversion and tagging. Default policy skips existing names without changing them; rename keeps both; overwrite replaces only when that policy is selected. Unverified preexisting files are excluded from the generated playlist.
- Up to three tracks transfer/convert concurrently. The UTF-8 extended M3U (`.m3u8`) lists verified successful files in original playlist order, including duplicates. Failed/unavailable positions are omitted without renumbering MP3s. Existing M3Us and reports are retained.
- A JSON report and in-app completion report list successful, skipped, unavailable, failed and cancelled entries. HTTP requests have timeouts and bounded retry/backoff for network failures, server errors and rate limits. Spotify tokens refresh automatically; rejected refresh tokens require reconnection.

## Cancel, retry and recovery

Cancel terminates conversion and stops new transfers. Completed MP3s remain. Job state and partial source data are stored inside the destination's `.listii/<job-id>` folder. A retry using the same playlist snapshot, source metadata, destination, quality, filename format and existing-file policy verifies completed MP3 hashes and reuses them. HTTP transfers resume with `Range` and `If-Range` when an ETag or Last-Modified validator is available; otherwise they restart safely. Converted-but-uncommitted outputs are recreated. Never delete `.listii` while a job is active.

After restarting the app, reconnect/load the same playlist, reattach the same source files or manifest, restore export options, and press Export. Sources and playlists are intentionally not stored in preferences. Changed URLs/options/snapshots create a new job; existing-file policy still applies. Stale locks left by a terminated process are recovered when that process no longer exists. An incomplete lock gives an explicit recovery instruction. An OS crash between committing an MP3 and recording its state may leave a valid but unverified file; skip keeps it, rename creates another, and overwrite requires your selected policy.

Partial source files remain after completion to support recovery. You can remove the job folder when no longer needed. Conversion progress is phase based (transfer bytes where available, conversion, tagging); it does not claim to measure the precise fraction of encoding time. Cancellation may wait for an in-flight network operation's timeout.

## Tests

```sh
python -m pytest -q
```

Tests exercise pagination with duplicates and removed items, URL validation, conservative matching/version conflicts, uncertain matches, source authorization validation, filename sanitization, real FFmpeg conversion, ID3 artwork/source fallback, original album numbering, M3U ordering under concurrency, existing-file policies, cancellation/retry, HTTP range recovery and changed remote files, stale locks, and authentication refresh/rate limits. Integration tests use synthesized test audio only; this is not an app audio provider. FFmpeg-dependent tests skip when FFmpeg is absent.

## Architecture and packaging

`models.py` holds explicit playlist positions; `spotify.py` handles OAuth and API access; `sources.py` contains adapters/matching; `metadata.py` maps tag fields; `export.py` owns resumable jobs and atomic output; `app.py` provides the native PySide6 UI with background workers and UI-thread signals. No web server is needed except the temporary loopback OAuth callback. Python dependencies are constrained in `pyproject.toml`; a reproducible tested environment is captured in `requirements-lock.txt` if supplied.

Build on the target operating system:

```sh
python -m pip install -e '.[build]'
pyinstaller --noconfirm --windowed --name Listii --paths . --collect-all keyring --add-data "listii/assets:listii/assets" desktop.py
```

macOS produces `dist/Listii.app`, Windows produces `dist/Listii/Listii.exe`, Linux produces `dist/Listii/Listii`. FFmpeg remains an external prerequisite. The macOS build is ad-hoc signed, without Developer ID signing or notarization; production distribution should add platform signing/notarization and installer packaging. Opening from a desktop launcher may require adding FFmpeg to its PATH; the provided entry point includes common macOS package-manager paths. Live Spotify sign-in/import requires your credentials and cannot be validated offline.

The supplied macOS build targets Apple Silicon and uses Python 3.12/OpenSSL 3.5 with PySide6 6.10 (macOS 13+). Its UI startup and the 22-test suite were verified locally. Live Spotify authorization and real authorized HTTPS audio-provider integration still require user credentials/source links. The app has no automatic audio-provider discovery.
