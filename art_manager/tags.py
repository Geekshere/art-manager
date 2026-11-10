"""
FLAC tags (Vorbis comments): reading, normalising, writing, backups.

Vorbis field names are case-insensitive by the spec, but tools happily write
the same field several ways — ALBUMARTIST, album_artist, "ALBUM ARTIST" — and
players then show duplicates or split albums. Everything here works on a
canonical, upper-case name per field (see canonical()), and normalize()
merges the variants, drops duplicate / empty values and trims whitespace.

Writing goes through the storage layer (local, SMB, WebDAV) and keeps the
embedded pictures. Every write first saves the old tags as a backup, so a
change can be undone.
"""
import json, os, re, struct, threading, time, uuid

from . import config, storage

# Canonical names (what Picard / most players use) by their "squashed" form:
# upper case without spaces, underscores or hyphens.
_CANON = [
    'TITLE', 'ARTIST', 'ARTISTS', 'ALBUM', 'ALBUMARTIST', 'ALBUMARTISTS', 'TRACKNUMBER', 'TRACKTOTAL',
    'DISCNUMBER', 'DISCTOTAL', 'DISCSUBTITLE', 'DATE', 'ORIGINALDATE', 'ORIGINALYEAR', 'GENRE', 'LABEL',
    'CATALOGNUMBER', 'BARCODE', 'ISRC', 'MEDIA', 'RELEASETYPE', 'RELEASESTATUS', 'RELEASECOUNTRY',
    'SCRIPT', 'LANGUAGE', 'COMPOSER', 'LYRICIST', 'CONDUCTOR', 'PERFORMER', 'COMMENT', 'COMPILATION',
    'ALBUMSORT', 'ALBUMARTISTSORT', 'ARTISTSORT', 'TITLESORT', 'COMPOSERSORT', 'ASIN', 'ACOUSTID_ID',
    'MUSICBRAINZ_TRACKID', 'MUSICBRAINZ_RELEASETRACKID', 'MUSICBRAINZ_ALBUMID', 'MUSICBRAINZ_RELEASEGROUPID',
    'MUSICBRAINZ_ARTISTID', 'MUSICBRAINZ_ALBUMARTISTID', 'MUSICBRAINZ_ALBUMCOMMENT', 'MUSICBRAINZ_WORKID',
    'REPLAYGAIN_TRACK_GAIN', 'REPLAYGAIN_TRACK_PEAK', 'REPLAYGAIN_ALBUM_GAIN', 'REPLAYGAIN_ALBUM_PEAK',
    'ENCODER', 'ENCODEDBY', 'COPYRIGHT', 'LYRICS', 'BPM', 'MOOD', 'WEBSITE',
]
_ALIASES = {   # other names for the same field
    'TOTALTRACKS': 'TRACKTOTAL', 'TOTALDISCS': 'DISCTOTAL', 'DISC': 'DISCNUMBER', 'YEAR': 'DATE',
    'CATALOGUENUMBER': 'CATALOGNUMBER', 'CATALOG': 'CATALOGNUMBER', 'ALBUMARTISTS': 'ALBUMARTISTS',
    'MUSICBRAINZRECORDINGID': 'MUSICBRAINZ_TRACKID', 'MUSICBRAINZRELEASEID': 'MUSICBRAINZ_ALBUMID',
    'ACOUSTIDID': 'ACOUSTID_ID',
}
_SQUASH = {re.sub(r'[\s_\-]', '', c): c for c in _CANON}
_SQUASH.update(_ALIASES)

# Fields that should hold exactly one value: two different values are a conflict.
SINGLE = {'TITLE', 'ALBUM', 'ALBUMARTIST', 'TRACKNUMBER', 'TRACKTOTAL', 'DISCNUMBER', 'DISCTOTAL', 'DATE',
          'ORIGINALDATE', 'ORIGINALYEAR', 'CATALOGNUMBER', 'BARCODE', 'MEDIA', 'RELEASETYPE', 'RELEASECOUNTRY',
          'MUSICBRAINZ_TRACKID', 'MUSICBRAINZ_RELEASETRACKID', 'MUSICBRAINZ_ALBUMID',
          'MUSICBRAINZ_RELEASEGROUPID', 'MUSICBRAINZ_ALBUMCOMMENT', 'COMPILATION', 'ALBUMSORT', 'TITLESORT'}

FRIENDLY = {'ALBUMARTIST': 'Album artist', 'TRACKNUMBER': 'Track number', 'TRACKTOTAL': 'Track total',
            'DISCNUMBER': 'Disc number', 'DISCTOTAL': 'Disc total', 'CATALOGNUMBER': 'Catalog number',
            'ORIGINALDATE': 'Original date', 'MUSICBRAINZ_ALBUMCOMMENT': 'Edition / version'}


def squash(key):
    return re.sub(r'[\s_\-]', '', key.upper())

def canonical(key):
    """'album_artist' / 'ALBUM ARTIST' / 'AlbumArtist' -> 'ALBUMARTIST'; unknown fields -> upper case."""
    k = key.strip()
    return _SQUASH.get(squash(k)) or k.upper()

def friendly(key):
    return FRIENDLY.get(key, key.title())


# ── reading ───────────────────────────────────────────────────────────────────

def parse_vorbis_comment(data):
    """Raw VORBIS_COMMENT block body -> [(key, value), ...] exactly as stored (case, order)."""
    out, p = [], 0
    vlen = struct.unpack_from('<I', data, p)[0]; p += 4 + vlen
    count = struct.unpack_from('<I', data, p)[0]; p += 4
    for _ in range(count):
        ln = struct.unpack_from('<I', data, p)[0]; p += 4
        kv = data[p:p + ln].decode('utf-8', 'replace'); p += ln
        k, sep, v = kv.partition('=')
        if sep:
            out.append((k, v))
    return out

def read_raw(path):
    """[(key, value)] of one FLAC, read without loading the pictures (cheap over the network)."""
    with storage.open_file(path, 'rb') as f:
        head = f.read(4)
        off = 4
        if head[:3] == b'ID3':
            rest = f.read(6)
            size = (rest[2] << 21) | (rest[3] << 14) | (rest[4] << 7) | rest[5]
            off = 10 + size
            f.seek(off)
            head = f.read(4)
            off += 4
        if head != b'fLaC':
            raise ValueError('not a FLAC file')
        while True:
            f.seek(off)
            h = f.read(4)
            if len(h) < 4:
                return []
            last, btype, blen = h[0] & 0x80, h[0] & 0x7F, int.from_bytes(h[1:4], 'big')
            if btype == 4:
                return parse_vorbis_comment(f.read(blen))
            if last:
                return []
            off += 4 + blen


# ── normalising ──

def fix_mojibake(v):
    """'StrÃ¤ngers' -> 'Strängers': UTF-8 text that was decoded as Latin-1/CP1252. Unchanged if unsure."""
    if not _MOJIBAKE.search(v) or '\ufffd' in v:
        return v
    for enc in ('cp1252', 'latin-1'):
        try:
            fixed = v.encode(enc).decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if not _MOJIBAKE.search(fixed):
            return fixed
    return v

def normalize(pairs):
    """
    [(key, value)] -> (ordered {CANONICAL: [values]}, [notes about what changed]).
    Merges spelling variants, drops duplicate (case-insensitive) and empty
    values, trims whitespace. For single-value fields with conflicting values
    the value stored under the canonical spelling wins (else the first one).
    """
    groups, spellings, notes = {}, {}, []
    for k, v in pairs:
        c = canonical(k)
        groups.setdefault(c, []).append((k, v))
        spellings.setdefault(c, [])
        if k not in spellings[c]:
            spellings[c].append(k)
    out = {}
    for c, items in groups.items():
        if len(spellings[c]) > 1:
            notes.append(f'{friendly(c)}: merged {", ".join(spellings[c])} into {c}')
        elif spellings[c][0] != c:
            notes.append(f'{spellings[c][0]} renamed to {c}')
        if c in SINGLE:   # canonical spelling first
            items = sorted(items, key=lambda kv: kv[0] != c)
        vals, seen = [], set()
        for _, v in items:
            t = v.strip()
            if t != v:
                notes.append(f'{friendly(c)}: removed spaces around "{t}"')
            f = fix_mojibake(t)
            if f != t:
                notes.append(f'{friendly(c)}: repaired garbled text "{t}" → "{f}"')
                t = f
            if not t:
                notes.append(f'{friendly(c)}: removed an empty value')
                continue
            if t.casefold() in seen:
                notes.append(f'{friendly(c)}: removed duplicate "{t}"')
                continue
            seen.add(t.casefold())
            vals.append(t)
        if c in SINGLE and len(vals) > 1:
            notes.append(f'{friendly(c)}: kept "{vals[0]}", dropped {", ".join(repr(x) for x in vals[1:])}')
            vals = vals[:1]
        if vals:
            out[c] = vals
    return out, list(dict.fromkeys(notes))

def as_pairs(tagdict):
    return [(k, v) for k, vals in tagdict.items() for v in vals]


# ── problems (used by the file check) ─────────────────────────────────────────

_MOJIBAKE = re.compile(r'Ã[\u0080-¿]|â€[\u0080-¿‘-›]|Â[ -¿]|�')
_TRACKNUM = re.compile(r'^\d{1,3}(/\d{1,3})?$')
_DATE = re.compile(r'^\d{4}(-\d{2}(-\d{2})?)?$')

def tag_problems(pairs):
    """Issues in one track's tags: [(level, code, message)]."""
    out = []
    groups = {}
    for k, v in pairs:
        groups.setdefault(canonical(k), []).append((k, v))
    for c, items in groups.items():
        names = list(dict.fromkeys(k for k, _ in items))
        if len(names) > 1:
            out.append(('warn', 'tag_spellings',
                        f'{friendly(c)} is stored under {len(names)} names ({", ".join(names)}) — players may show it twice'))
        vals = [v.strip() for _, v in items if v.strip()]
        folded = [v.casefold() for v in vals]
        if len(folded) != len(set(folded)):
            out.append(('warn', 'dup_values', f'{friendly(c)} has the same value more than once'))
        if c in SINGLE and len(set(folded)) > 1:
            out.append(('warn', 'tag_conflict', f'{friendly(c)} has different values: ' +
                        ', '.join(f'"{v}"' for v in dict.fromkeys(vals))[:200]))
        if any(v != v.strip() for _, v in items):
            out.append(('info', 'tag_whitespace', f'{friendly(c)} has spaces at the start or end'))
        if any(not v.strip() for _, v in items):
            out.append(('info', 'empty_tag', f'{friendly(c)} is empty'))
        if any(_MOJIBAKE.search(v) for _, v in items):
            bad = next(v for _, v in items if _MOJIBAKE.search(v))
            out.append(('warn', 'mojibake', f'{friendly(c)} looks garbled ("{bad[:60]}") — probably a text-encoding mix-up'))
    tn = [v.strip() for _, v in groups.get('TRACKNUMBER', []) if v.strip()]
    if tn and not _TRACKNUM.match(tn[0]):
        out.append(('warn', 'bad_tracknumber', f'Track number "{tn[0]}" is not a number'))
    for f in ('DATE', 'ORIGINALDATE'):
        d = [v.strip() for _, v in groups.get(f, []) if v.strip()]
        if d and not _DATE.match(d[0]):
            out.append(('info', 'odd_date', f'{friendly(f)} "{d[0]}" is not in YYYY or YYYY-MM-DD form'))
    if pairs and all(k == k.lower() for k, _ in pairs) and not any(lvl == 'warn' and code == 'tag_spellings' for lvl, code, _ in out):
        out.append(('info', 'key_case', 'Field names are lower case (fine for most players; Clean up makes them upper case)'))
    return out

CLEANUP_CODES = {'tag_spellings', 'dup_values', 'tag_whitespace', 'empty_tag', 'key_case', 'tag_conflict'}


# ── writing, backups, undo ────────────────────────────────────────────────────

_backup_lock = threading.Lock()

def _backup_dir():
    d = os.path.join(config.DATA_DIR, 'tag_backups')
    os.makedirs(d, exist_ok=True)
    return d

def write(path, tags):
    """
    Replace all tags of one FLAC; pictures are kept. `tags` is {KEY: [values]}
    or a raw [(key, value)] list — the raw form keeps spellings exactly as
    given (needed to undo to a state with ALBUMARTIST *and* album_artist).
    """
    from mutagen.flac import FLAC
    pairs = as_pairs(tags) if isinstance(tags, dict) else list(tags)
    with storage.open_file(path, 'rb+') as f:
        audio = FLAC(f)
        if audio.tags is None:
            audio.add_tags()
        del audio.tags[:]                 # list-level: VCommentDict's own setters merge cases
        for k, v in pairs:
            audio.tags.append((k, v))
        f.seek(0)
        audio.save(f)

def apply(changes, label=''):
    """
    changes: [{'path': ..., 'tags': {KEY: [values]}}] — the complete new tags per file.
    Saves the old tags first; returns the backup id (for undo).
    """
    backup = {'id': time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6], 'label': label,
              'created': time.time(), 'files': []}
    for ch in changes:
        backup['files'].append({'path': ch['path'], 'tags': read_raw(ch['path'])})
    with _backup_lock:
        with open(os.path.join(_backup_dir(), backup['id'] + '.json'), 'w', encoding='utf-8') as f:
            json.dump(backup, f)
        _prune_backups()
    errors = []
    for ch in changes:
        try:
            write(ch['path'], ch['tags'])
        except Exception as e:
            errors.append({'path': ch['path'], 'error': storage.error_message(e, ch['path'])})
    return backup['id'], errors

def _prune_backups(keep=200):
    files = sorted(os.listdir(_backup_dir()))
    for name in files[:-keep]:
        try: os.remove(os.path.join(_backup_dir(), name))
        except OSError: pass

def list_backups(path_prefix=None):
    out = []
    for name in sorted(os.listdir(_backup_dir()), reverse=True):
        try:
            with open(os.path.join(_backup_dir(), name), encoding='utf-8') as f:
                b = json.load(f)
        except Exception:
            continue
        if path_prefix and not any(x['path'].startswith(path_prefix) for x in b['files']):
            continue
        out.append({'id': b['id'], 'label': b.get('label', ''), 'created': b['created'], 'files': len(b['files'])})
    return out

def undo(backup_id):
    """Write the tags saved in a backup back to its files."""
    if not re.match(r'^[\w.-]+$', backup_id or ''):
        raise ValueError('bad backup id')
    with open(os.path.join(_backup_dir(), backup_id + '.json'), encoding='utf-8') as f:
        b = json.load(f)
    errors = []
    for x in b['files']:
        try:
            write(x['path'], [tuple(kv) for kv in x['tags']])
        except Exception as e:
            errors.append({'path': x['path'], 'error': str(e)})
    return b, errors
