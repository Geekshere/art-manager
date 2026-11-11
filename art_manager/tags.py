"""
Tags (FLAC Vorbis comments, and via formats.py every other format): reading, normalising, writing, backups.

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
    'ENCODER', 'ENCODEDBY', 'COPYRIGHT', 'LYRICS', 'BPM', 'MOOD', 'WEBSITE', 'GROUPING', 'SUBTITLE', 'REMIXER',
]
_ALIASES = {   # other names for the same field
    'DISC': 'DISCNUMBER', 'YEAR': 'DATE',
    'CATALOGUENUMBER': 'CATALOGNUMBER', 'CATALOG': 'CATALOGNUMBER', 'ALBUMARTISTS': 'ALBUMARTISTS',
    'MUSICBRAINZRECORDINGID': 'MUSICBRAINZ_TRACKID', 'MUSICBRAINZRELEASEID': 'MUSICBRAINZ_ALBUMID',
    'ACOUSTIDID': 'ACOUSTID_ID', 'TRACK': 'TRACKNUMBER',
    # Picard's names in MP3 TXXX frames / M4A freeform atoms / WMA attributes
    'MUSICBRAINZALBUMTYPE': 'RELEASETYPE', 'MUSICBRAINZALBUMSTATUS': 'RELEASESTATUS',
    'MUSICBRAINZALBUMRELEASECOUNTRY': 'RELEASECOUNTRY',
}
# Fields that Picard / Lidarr deliberately write under a second name as well,
# with the same value, because different players read different names. They
# are kept as their own fields (never merged away); only a *different* value
# is a problem, and saving keeps them in step with the main field.
COMPANIONS = {'TOTALTRACKS': 'TRACKTOTAL', 'TOTALDISCS': 'DISCTOTAL', 'MUSICBRAINZ_ALBUMTYPE': 'RELEASETYPE',
              'MUSICBRAINZ_ALBUMSTATUS': 'RELEASESTATUS', 'MUSICBRAINZ_ALBUMRELEASECOUNTRY': 'RELEASECOUNTRY',
              'YEAR': 'DATE'}          # YEAR holds the year of DATE

def _companion_value(comp, main_vals):
    return [main_vals[0][:4]] if comp == 'YEAR' and main_vals else list(main_vals)

def _companion_agrees(comp, comp_vals, main_vals):
    a = {v.strip().casefold() for v in comp_vals if v.strip()}
    b = {v.strip().casefold() for v in main_vals if v.strip()}
    if comp == 'YEAR':
        b = {v[:4] for v in b}
    return not a or not b or a == b

# Values that mean "nothing" (MusicBrainz writes [none] for a missing catalogue number)
PLACEHOLDERS = {'[none]', 'none', 'n/a', '[no label]', '[unknown]', 'unknown', '-'}
# Fields where two different values are always a problem. Other "single" fields
# (album artist, release type, ...) may hold several values on purpose; for
# those only values that disagree across different spellings are reported.
STRICT_SINGLE = {'TITLE', 'ALBUM', 'TRACKNUMBER', 'TRACKTOTAL', 'DISCNUMBER', 'DISCTOTAL', 'COMPILATION',
                 'MUSICBRAINZ_TRACKID', 'MUSICBRAINZ_RELEASETRACKID', 'MUSICBRAINZ_ALBUMID',
                 'MUSICBRAINZ_RELEASEGROUPID'}
_DATES = {'DATE', 'ORIGINALDATE'}

def _dates_agree(vals):
    """'2024' and '2024-05-14' agree (one is the start of the other)."""
    vals = sorted({v.strip() for v in vals if v.strip()}, key=len)
    return all(vals[-1].startswith(v) for v in vals)
_COMP_SQUASH = {re.sub(r'[\s_\-]', '', c): c for c in COMPANIONS}

_SQUASH = {re.sub(r'[\s_\-]', '', c): c for c in _CANON}
_SQUASH.update(_ALIASES)

# Fields that should hold exactly one value: two different values are a conflict.
SINGLE = {'TITLE', 'ALBUM', 'ALBUMARTIST', 'TRACKNUMBER', 'TRACKTOTAL', 'DISCNUMBER', 'DISCTOTAL', 'DATE',
          'ORIGINALDATE', 'ORIGINALYEAR', 'BARCODE', 'MEDIA', 'RELEASETYPE', 'RELEASECOUNTRY',
          'MUSICBRAINZ_TRACKID', 'MUSICBRAINZ_RELEASETRACKID', 'MUSICBRAINZ_ALBUMID',
          'MUSICBRAINZ_RELEASEGROUPID', 'MUSICBRAINZ_ALBUMCOMMENT', 'COMPILATION', 'ALBUMSORT', 'TITLESORT'}

FRIENDLY = {'ALBUMARTIST': 'Album artist', 'TRACKNUMBER': 'Track number', 'TRACKTOTAL': 'Track total',
            'DISCNUMBER': 'Disc number', 'DISCTOTAL': 'Disc total', 'CATALOGNUMBER': 'Catalog number',
            'ORIGINALDATE': 'Original date', 'MUSICBRAINZ_ALBUMCOMMENT': 'Edition / version',
            'RELEASETYPE': 'Release type', 'RELEASESTATUS': 'Release status', 'RELEASECOUNTRY': 'Release country',
            'ALBUMARTISTSORT': 'Album artist sort', 'ARTISTSORT': 'Artist sort', 'ORIGINALYEAR': 'Original year'}


def squash(key):
    return re.sub(r'[\s_\-]', '', key.upper())

def canonical(key):
    """
    'album_artist' / 'ALBUM ARTIST' / 'AlbumArtist' -> 'ALBUMARTIST'; also the
    native names of other formats ('TPE2', 'TXXX:Album Artist', 'aART',
    'WM/AlbumArtist' -> 'ALBUMARTIST'). Unknown Vorbis fields -> upper case;
    unknown native fields ('TXXX:iTunNORM', 'TKEY') -> unchanged.
    """
    from .formats import native_canonical
    k = key.strip()
    if k == 'Year':                 # APEv2's name for the date (WavPack, Monkey's Audio)
        return 'DATE'
    n = native_canonical(k)
    if n is not None:
        return _SQUASH.get(squash(n)) or (n if n in _CANON else k)
    sq = squash(k)
    return _COMP_SQUASH.get(sq) or _SQUASH.get(sq) or k.upper()

def _is_native(key, canon=None):
    """
    A format's own name for a standard field (TPE2, aART, WM/AlbumArtist,
    TXXX:MusicBrainz Album Id): written back exactly the same way.
    """
    from .formats import APE_NAMES, TXXX_NAMES, native_canonical
    if key.startswith(('TXXX:', '----:')):
        desc = key.split(':', 2)[-1] if key.startswith('----:') else key[5:]
        return canon is not None and desc == TXXX_NAMES.get(canon, canon)
    return native_canonical(key) is not None or key in APE_NAMES.values() or key in ('Track', 'Disc')

def friendly(key):
    """'ALBUMARTIST' -> 'Album artist'; unknown fields are shown exactly as named."""
    if key in FRIENDLY:
        return FRIENDLY[key]
    if key in _CANON and '_' not in key:
        return key.capitalize()
    return key


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
    """[(key, value)] of one file as stored; FLACs are read without loading the pictures."""
    from . import formats
    if formats.family(path) != 'flac':
        return formats.read_raw(path)
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
        elif spellings[c][0] != c and not _is_native(spellings[c][0], c):
            notes.append(f'{spellings[c][0]} renamed to {c}')
        if c in SINGLE:   # canonical spelling first, then in the order found
            order = {k: i for i, k in enumerate(spellings[c])}
            items = sorted(items, key=lambda kv: (kv[0] != c, order[kv[0]]))
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
        if c in _DATES and len(vals) > 1 and _dates_agree(vals):
            full = max(vals, key=len)          # '2024' + '2024-05-14' -> the full date
            notes.append(f'{friendly(c)}: kept the full date "{full}"')
            vals = [full]
        elif c in STRICT_SINGLE and len(vals) > 1:
            notes.append(f'{friendly(c)}: kept "{vals[0]}", dropped {", ".join(repr(x) for x in vals[1:])}')
            vals = vals[:1]
        elif c in SINGLE and len(spellings[c]) > 1 and len(vals) > 1:
            # several spellings disagree: keep what the preferred spelling says (all its values)
            first = items[0][0]
            keep = {v.strip().casefold() for k, v in items if k == first}
            kept = [v for v in vals if v.casefold() in keep] or vals[:1]
            if len(kept) < len(vals):
                notes.append(f'{friendly(c)}: kept {", ".join(repr(x) for x in kept)} ({first}), '
                             f'dropped {", ".join(repr(x) for x in vals if x not in kept)}')
            vals = kept
        if vals:
            out[c] = vals
    # '3/12' in the number field (how MP3 / M4A store it): number and total apart
    for num, tot in (('TRACKNUMBER', 'TRACKTOTAL'), ('DISCNUMBER', 'DISCTOTAL')):
        v = out.get(num, [''])[0]
        m = re.match(r'^\s*(\d+)\s*/\s*(\d+)\s*$', v)
        if m:
            out[num] = [m.group(1)]
            if m.group(2) != '0':
                out.setdefault(tot, [m.group(2)])
            notes.append(f'{friendly(num)}: "{v}" split into number and total')
    for comp, main in COMPANIONS.items():          # TOTALTRACKS follows TRACKTOTAL, YEAR follows DATE, ...
        if comp in out and main in out and not _companion_agrees(comp, out[comp], out[main]):
            new = _companion_value(comp, out[main])
            notes.append(f'{comp}: "{"; ".join(out[comp])}" set to match {friendly(main).lower()} "{"; ".join(new)}"')
            out[comp] = new
    return out, list(dict.fromkeys(notes))

def sync_companions(tagdict):
    """Before writing: TOTALTRACKS etc. take the main field's value, or go when it is removed."""
    out = dict(tagdict)
    for comp, main in COMPANIONS.items():
        if comp in out:
            if out.get(main):
                out[comp] = _companion_value(comp, out[main])
            else:
                del out[comp]
    return out

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
    conflicts = set()
    for c, items in groups.items():
        names = list(dict.fromkeys(k for k, _ in items))
        real = lambda vs: {v.strip().casefold() for v in vs if v.strip() and v.strip().casefold() not in PLACEHOLDERS}
        per_name = {n: real(v for k, v in items if k == n) for n in names}
        if len(names) > 1:
            sets = [x for x in per_name.values() if x]
            agree = len({frozenset(x) for x in sets}) <= 1 or (c in _DATES and _dates_agree(set().union(*sets)))
            if agree:
                out.append(('info', 'tag_spellings', f'{friendly(c)} is written under {len(names)} names '
                            f'({", ".join(names)}) that agree — harmless; Clean up keeps one'))
            else:
                conflicts.add(c)
                shown = '; '.join(f'{n} "{next(v.strip() for k, v in items if k == n and v.strip())}"'
                                  for n in names if per_name[n])
                out.append(('warn', 'tag_conflict', f'{friendly(c)} differs between its names: {shown}'[:300]))
        else:
            vals = [v.strip() for _, v in items if v.strip()]
            folded = [v.casefold() for v in vals]
            if len(folded) != len(set(folded)):
                out.append(('info', 'dup_values', f'{friendly(c)} has the same value more than once'))
            distinct = real(vals)
            if c in _DATES and len(distinct) > 1 and not _dates_agree(distinct):
                conflicts.add(c)
                out.append(('warn', 'tag_conflict', f'{friendly(c)} has different values: ' +
                            ', '.join(f'"{v}"' for v in dict.fromkeys(vals))[:200]))
            elif c in STRICT_SINGLE and len(distinct) > 1:
                conflicts.add(c)
                out.append(('warn', 'tag_conflict', f'{friendly(c)} has different values: ' +
                            ', '.join(f'"{v}"' for v in dict.fromkeys(vals))[:200]))
        if any(v != v.strip() for _, v in items):
            out.append(('info', 'tag_whitespace', f'{friendly(c)} has spaces at the start or end'))
        if any(not v.strip() for _, v in items):
            out.append(('info', 'empty_tag', f'{friendly(c)} is empty'))
        if any(_MOJIBAKE.search(v) for _, v in items):
            bad = next(v for _, v in items if _MOJIBAKE.search(v))
            out.append(('warn', 'mojibake', f'{friendly(c)} looks garbled ("{bad[:60]}") — probably a text-encoding mix-up'))
    for comp, main in COMPANIONS.items():
        cv = [v for _, v in groups.get(comp, [])]
        mv = [v for _, v in groups.get(main, [])]
        if main in conflicts or comp in conflicts or _companion_agrees(comp, cv, mv):
            continue
        out.append(('warn', 'companion_mismatch', f'{friendly(main)} is "{mv[0].strip()}" but '
                    f'{groups[comp][0][0]} says "{cv[0].strip()}" — players may disagree'))
    tn = [v.strip() for _, v in groups.get('TRACKNUMBER', []) if v.strip()]
    if tn and not _TRACKNUM.match(tn[0]):
        out.append(('warn', 'bad_tracknumber', f'Track number "{tn[0]}" is not a number'))
    for f in ('DATE', 'ORIGINALDATE'):
        d = [v.strip() for _, v in groups.get(f, []) if v.strip()]
        if d and not _DATE.match(d[0]):
            out.append(('info', 'odd_date', f'{friendly(f)} "{d[0]}" is not in YYYY or YYYY-MM-DD form'))
    vorbis_keys = [k for k, _ in pairs if not _is_native(k) and ':' not in k]
    if vorbis_keys and all(k == k.lower() for k in vorbis_keys) and not any(code == 'tag_spellings' for _, code, _ in out):
        out.append(('info', 'key_case', 'Field names are lower case (fine for most players; Clean up makes them upper case)'))
    return out

CLEANUP_CODES = {'tag_spellings', 'dup_values', 'tag_whitespace', 'empty_tag', 'key_case', 'tag_conflict',
                 'companion_mismatch'}


# ── writing, backups, undo ────────────────────────────────────────────────────

_backup_lock = threading.Lock()

def _backup_dir():
    d = os.path.join(config.DATA_DIR, 'tag_backups')
    os.makedirs(d, exist_ok=True)
    return d

def write(path, tags):
    """
    Replace all tags of one file; pictures are kept. `tags` is {KEY: [values]}
    (written under the format's usual field names) or a raw [(key, value)]
    list — the raw form keeps spellings exactly as given (needed to undo to a
    state with ALBUMARTIST *and* album_artist).
    """
    from mutagen.flac import FLAC
    from . import formats
    if isinstance(tags, dict):
        tags = sync_companions(tags)
    if formats.family(path) != 'flac':
        pairs = formats.to_native(path, tags) if isinstance(tags, dict) else list(tags)
        return formats.write(path, pairs)
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
