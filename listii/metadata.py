import base64
from mutagen.flac import Picture
import mutagen
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TPE2, TDRC, TCON, TPOS, TRCK, TSRC, APIC

FIELDS = {"title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2,
          "date": TDRC, "genre": TCON, "discnumber": TPOS, "tracknumber": TRCK, "isrc": TSRC}

def source_metadata(path):
    media = mutagen.File(path, easy=True)
    fields = {key: list(media.tags.get(key, [])) for key in FIELDS} if media and media.tags else {}
    artwork = None
    raw = mutagen.File(path)
    tags = raw.tags if raw else None
    if tags:
        if hasattr(tags, "getall"):
            for key, frame in FIELDS.items():
                if not fields.get(key):
                    frames = tags.getall(frame.__name__)
                    if frames:
                        fields[key] = [str(value) for value in frames[0].text]
        if hasattr(tags, "getall"):
            pictures = tags.getall("APIC")
            if pictures:
                artwork = (pictures[0].data, pictures[0].mime)
        elif "covr" in tags:
            cover = tags["covr"][0]
            artwork = (bytes(cover), "image/png" if cover.imageformat == 14 else "image/jpeg")
    if not artwork and tags and tags.get("metadata_block_picture"):
        try:
            picture = Picture(base64.b64decode(tags["metadata_block_picture"][0]))
            artwork = (picture.data, picture.mime)
        except (ValueError, TypeError):
            pass
    if not artwork and raw and getattr(raw, "pictures", None):
        picture = raw.pictures[0]
        artwork = (picture.data, picture.mime)
    return fields, artwork

def embed(path, track, source_path, artwork=None):
    fields, source_art = source_metadata(source_path)
    overrides = {"title": [track.title], "artist": track.artists, "album": [track.album] if track.album else [],
                 "albumartist": track.album_artists, "date": [track.year] if track.year else [],
                 "discnumber": [str(track.disc)] if track.disc else [],
                 "tracknumber": [str(track.album_track)] if track.album_track else [],
                 "isrc": [track.isrc] if track.isrc else []}
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    for key, frame in FIELDS.items():
        values = overrides.get(key) or fields.get(key)
        if values:
            tags.delall(frame.__name__)
            tags.add(frame(encoding=3, text=values))
    art = artwork or source_art
    if art:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=art[1], type=3, desc="Album cover", data=art[0]))
    tags.save(path, v2_version=3)
