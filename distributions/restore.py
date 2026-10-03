"""Reassemble the verified Listii macOS build from GitHub's upload parts."""
import hashlib
import json
import os
import shutil
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    folder = Path(__file__).resolve().parent
    manifest = json.loads((folder / 'manifest.json').read_text())
    output = folder / manifest['filename']
    if output.exists():
        if digest(output) == manifest['sha256']:
            print('Already verified: ' + str(output))
            return
        raise SystemExit('An existing ZIP differs. Move it elsewhere before restoring this build.')
    temp = output.with_suffix('.zip.partial')
    try:
        with temp.open('xb') as destination:
            for part in manifest['parts']:
                path = folder / part['filename']
                if not path.is_file() or path.stat().st_size != part['bytes'] or digest(path) != part['sha256']:
                    raise SystemExit('Missing or damaged build part: ' + path.name + '. Download/clone the complete repository again.')
                with path.open('rb') as source:
                    shutil.copyfileobj(source, destination)
        if temp.stat().st_size != manifest['bytes'] or digest(temp) != manifest['sha256']:
            raise SystemExit('Reassembled archive failed verification.')
        # Publish without replacing any pre-existing archive.
        os.link(temp, output)
        print('Verified: ' + str(output))
        print('Unzip it, then open Listii.app. FFmpeg is required for MP3 conversion.')
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
