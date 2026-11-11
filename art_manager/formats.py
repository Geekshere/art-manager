"""
Audio formats other than FLAC: MP3, AAC/ALAC (.m4a), Ogg Vorbis, Opus,
WavPack, Monkey's Audio, Musepack, AIFF, WAV, DSD (.dsf) and WMA.

Every format is read into the same shape as FLAC tags: a list of
(key, value) pairs, where key is the format's own name for the field
('TPE2', 'TXXX:ALBUMARTIST', '©ART', '----:com.apple.iTunes:LABEL',
'Album Artist', 'WM/AlbumArtist'). tags.canonical() maps every one of those to
the same canonical name (ALBUMARTIST), so duplicates such as TPE2 *and*
TXXX:ALBUM ARTIST in one MP3 are found and merged just like ALBUMARTIST and
album_artist in a FLAC.

Writing takes either canonical tags ({'ALBUMARTIST': [...]}, written under the
names Picard uses for that format) or raw pairs (written back exactly, for
undo). Only text fields are replaced: pictures and other binary data stay.
"""
import base64, io, os

from . import storage

# extension -> tag family
FAMILY = {
    '.flac': 'flac',
    '.ogg': 'vorbis', '.oga': 'vorbis', '.opus': 'vorbis', '.spx': 'vorbis',
    '.mp3': 'id3', '.aif': 'id3', '.aiff': 'id3', '.aifc': 'id3', '.wav': 'id3', '.dsf': 'id3',
    '.m4a': 'mp4', '.m4b': 'mp4',
    '.wv': 'ape', '.ape': 'ape', '.mpc': 'ape',
    '.wma': 'asf',
}
AUDIO_EXTS = tuple(FAMILY)
LOSSY = ('.mp3', '.ogg', '.oga', '.opus', '.spx', '.mpc', '.wma')     # .m4a may be either
FORMAT_NAME = {'.flac': 'FLAC', '.mp3': 'MP3', '.m4a': 'M4A', '.m4b': 'M4B',
               '.ogg': 'Ogg Vorbis', '.oga': 'Ogg', '.opus': 'Opus', '.spx': 'Speex', '.wv': 'WavPack',
               '.ape': "Monkey's Audio", '.mpc': 'Musepack', '.aif': 'AIFF', '.aiff': 'AIFF', '.aifc': 'AIFF',
               '.wav': 'WAV', '.dsf': 'DSF', '.wma': 'WMA'}


def ext(path):
    return os.path.splitext(path)[1].lower()

def family(path):
    return FAMILY.get(ext(path))

def is_audio(name):
    return ext(name) in FAMILY


def _mutagen_class(path):
    e = ext(path)
    if e == '.flac':
        from mutagen.flac import FLAC as C
    elif e == '.mp3':
        from mutagen.mp3 import MP3 as C
    elif e in ('.aif', '.aiff', '.aifc'):
        from mutagen.aiff import AIFF as C
    elif e == '.wav':
        from mutagen.wave import WAVE as C
    elif e == '.dsf':
        from mutagen.dsf import DSF as C
    elif e in ('.m4a', '.m4b'):
        from mutagen.mp4 import MP4 as C
    elif e in ('.ogg', '.oga'):
        import mutagen.oggvorbis, mutagen.oggflac
        return None     # let mutagen pick: Vorbis or Ogg FLAC
    elif e == '.opus':
        from mutagen.oggopus import OggOpus as C
    elif e == '.spx':
        from mutagen.oggspeex import OggSpeex as C
    elif e == '.wv':
        from mutagen.wavpack import WavPack as C
    elif e == '.ape':
        from mutagen.monkeysaudio import MonkeysAudio as C
    elif e == '.mpc':
        from mutagen.musepack import Musepack as C
    elif e == '.wma':
        from mutagen.asf import ASF as C
    else:
        raise ValueError(f'unsupported audio format: {e or "no extension"}')
    return C

def load(f, path):
    """Open an already opened file object with the right mutagen class."""
    C = _mutagen_class(path)
    if C is None:
        import mutagen
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggflac import OggFLAC
        from mutagen.oggopus import OggOpus
        a = mutagen.File(f, options=[OggVorbis, OggFLAC, OggOpus])
        if a is None:
            raise ValueError('not a recognised Ogg file')
        return a
    return C(f)

def open_audio(path):
    with storage.open_file(path, 'rb') as f:
        return load(f, path)


# ── ID3 (MP3, AIFF, WAV, DSF) ─────────────────────────────────────────────────

ID3_FRAMES = {   # frame -> canonical (Picard's mapping)
    'TIT2': 'TITLE', 'TPE1': 'ARTIST', 'TALB': 'ALBUM', 'TPE2': 'ALBUMARTIST', 'TRCK': 'TRACKNUMBER',
    'TPOS': 'DISCNUMBER', 'TDRC': 'DATE', 'TYER': 'DATE', 'TDOR': 'ORIGINALDATE', 'TORY': 'ORIGINALYEAR',
    'TCON': 'GENRE', 'TCOM': 'COMPOSER', 'TEXT': 'LYRICIST', 'TPE3': 'CONDUCTOR', 'TSST': 'DISCSUBTITLE',
    'TPUB': 'LABEL', 'TSRC': 'ISRC', 'TMED': 'MEDIA', 'TSOA': 'ALBUMSORT', 'TSOP': 'ARTISTSORT',
    'TSO2': 'ALBUMARTISTSORT', 'TSOT': 'TITLESORT', 'TSOC': 'COMPOSERSORT', 'TENC': 'ENCODEDBY',
    'TSSE': 'ENCODER', 'TCOP': 'COPYRIGHT', 'TBPM': 'BPM', 'TMOO': 'MOOD', 'TLAN': 'LANGUAGE',
    'TCMP': 'COMPILATION', 'TIT1': 'GROUPING', 'TIT3': 'SUBTITLE', 'TPE4': 'REMIXER',
}
_ID3_WRITE = {v: k for k, v in ID3_FRAMES.items() if k not in ('TYER', 'TORY')}
# canonical -> TXXX description, as Picard writes them (same names in MP4 freeform atoms)
TXXX_NAMES = {
    'ALBUMARTISTS': 'ALBUMARTISTS', 'ARTISTS': 'ARTISTS', 'CATALOGNUMBER': 'CATALOGNUMBER', 'BARCODE': 'BARCODE',
    'SCRIPT': 'SCRIPT', 'ASIN': 'ASIN', 'RELEASETYPE': 'MusicBrainz Album Type',
    'RELEASESTATUS': 'MusicBrainz Album Status', 'RELEASECOUNTRY': 'MusicBrainz Album Release Country',
    'MUSICBRAINZ_ALBUMID': 'MusicBrainz Album Id', 'MUSICBRAINZ_RELEASEGROUPID': 'MusicBrainz Release Group Id',
    'MUSICBRAINZ_ARTISTID': 'MusicBrainz Artist Id', 'MUSICBRAINZ_ALBUMARTISTID': 'MusicBrainz Album Artist Id',
    'MUSICBRAINZ_RELEASETRACKID': 'MusicBrainz Release Track Id', 'MUSICBRAINZ_WORKID': 'MusicBrainz Work Id',
    'MUSICBRAINZ_ALBUMCOMMENT': 'MusicBrainz Album Comment', 'MUSICBRAINZ_TRACKID': 'MusicBrainz Track Id',
    'ACOUSTID_ID': 'Acoustid Id', 'ORIGINALYEAR': 'originalyear', 'WEBSITE': 'WEBSITE',
}
MB_UFID = 'http://musicbrainz.org'
_ID3_MANAGED = ('T', 'COMM', 'USLT', 'UFID')     # frames replaced on write; APIC, PRIV, ... stay

def _id3_pairs(tags):
    out = []
    for frame in tags.values():
        fid = frame.FrameID
        if fid == 'UFID':
            if frame.owner == MB_UFID:
                out.append(('UFID:' + MB_UFID, frame.data.decode('ascii', 'replace')))
            continue
        if fid == 'TXXX':
            out += [('TXXX:' + frame.desc, str(t)) for t in frame.text]
        elif fid in ('COMM', 'USLT'):
            key = f'{fid}:{frame.desc}:{frame.lang}'
            texts = frame.text if isinstance(frame.text, list) else [frame.text]
            out += [(key, str(t)) for t in texts]
        elif fid == 'TCON':
            out += [(fid, g) for g in frame.genres]
        elif fid.startswith('T') and hasattr(frame, 'text'):
            out += [(fid, str(t)) for t in frame.text]
    return out

def id3_canonical(key):
    """Canonical name for an ID3 key, or None if it isn't one."""
    if key in ID3_FRAMES:
        return ID3_FRAMES[key]
    if key.startswith('TXXX:'):
        return key[5:]                          # handed to canonical() by the caller
    if key.startswith('COMM:'):
        desc = key.split(':')[1]
        return 'COMMENT' if not desc else key
    if key.startswith('USLT:'):
        return 'LYRICS'
    if key == 'UFID:' + MB_UFID:
        return 'MUSICBRAINZ_TRACKID'
    if len(key) == 4 and key[0] == 'T' and key.isupper() and key.isalnum():
        return key                              # another text frame (TKEY, TOAL, ...): kept as is
    return None

def _canonical_to_id3(tagdict):
    """{CANONICAL: [values]} -> [(id3 key, value)] using Picard's frames."""
    t = dict(tagdict)
    out = []
    num = lambda n, tot: f"{n[0]}/{tot[0]}" if n and tot else (n[0] if n else '')
    trck = num(t.pop('TRACKNUMBER', []), t.pop('TRACKTOTAL', []))
    tpos = num(t.pop('DISCNUMBER', []), t.pop('DISCTOTAL', []))
    if trck: out.append(('TRCK', trck))
    if tpos: out.append(('TPOS', tpos))
    for k, vals in t.items():
        if k in _ID3_WRITE:
            key = _ID3_WRITE[k]
        elif k == 'COMMENT':
            key = 'COMM::eng'
        elif k == 'LYRICS':
            key = 'USLT::eng'
        elif k == 'MUSICBRAINZ_TRACKID':
            key = 'UFID:' + MB_UFID
        elif ':' in k and k.split(':')[0] in ('COMM', 'USLT', 'TXXX'):
            key = k                              # unmapped native key kept as is
        elif len(k) == 4 and k[0] == 'T' and k.isalnum() and _is_id3_text_frame(k):
            key = k
        else:
            key = 'TXXX:' + TXXX_NAMES.get(k, k)
        out += [(key, v) for v in vals]
    return out

def _is_id3_text_frame(fid):
    from mutagen import id3
    cls = getattr(id3, fid, None)
    return isinstance(cls, type) and issubclass(cls, id3.TextFrame)

def _write_id3(audio, pairs):
    from mutagen import id3
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for key in list(tags.keys()):
        fid = key[:4]
        if fid.startswith(_ID3_MANAGED[0]) or fid in _ID3_MANAGED[1:]:
            if fid == 'UFID' and key != 'UFID:' + MB_UFID:
                continue
            del tags[key]
    grouped = {}
    for k, v in pairs:
        grouped.setdefault(k, []).append(v)
    for key, vals in grouped.items():
        if key.startswith('TXXX:'):
            tags.add(id3.TXXX(encoding=3, desc=key[5:], text=vals))
        elif key.startswith(('COMM:', 'USLT:')):
            fid, desc, lang = (key.split(':', 2) + ['', ''])[:3]
            lang = (lang or 'eng')[:3].ljust(3)
            if fid == 'COMM':
                tags.add(id3.COMM(encoding=3, desc=desc, lang=lang, text=vals))
            else:
                tags.add(id3.USLT(encoding=3, desc=desc, lang=lang, text='\n'.join(vals)))
        elif key == 'UFID:' + MB_UFID:
            tags.add(id3.UFID(owner=MB_UFID, data=vals[0].encode('ascii', 'replace')))
        elif len(key) == 4 and key.startswith('T') and _is_id3_text_frame(key):
            if key in ('TDRC', 'TDOR', 'TYER', 'TORY'):
                vals = [v for v in vals if v]
            tags.add(getattr(id3, key)(encoding=3, text=vals))
        else:
            tags.add(id3.TXXX(encoding=3, desc=key, text=vals))

def _id3_version(audio):
    v = getattr(audio.tags, 'version', (2, 4, 0)) if audio.tags is not None else (2, 4, 0)
    return 3 if v[1] == 3 else 4      # keep v2.3 files v2.3 (older players / Windows)


# ── MP4 (.m4a) ────────────────────────────────────────────────────────────────

MP4_ATOMS = {
    '©nam': 'TITLE', '©ART': 'ARTIST', '©alb': 'ALBUM', 'aART': 'ALBUMARTIST', '©day': 'DATE',
    '©gen': 'GENRE', '©wrt': 'COMPOSER', '©cmt': 'COMMENT', '©lyr': 'LYRICS', '©grp': 'GROUPING',
    'trkn': 'TRACKNUMBER', 'disk': 'DISCNUMBER', 'cpil': 'COMPILATION', 'tmpo': 'BPM',
    'soal': 'ALBUMSORT', 'soar': 'ARTISTSORT', 'soaa': 'ALBUMARTISTSORT', 'sonm': 'TITLESORT',
    'soco': 'COMPOSERSORT', 'cprt': 'COPYRIGHT', '©too': 'ENCODER', '©enc': 'ENCODEDBY', 'desc': 'DESCRIPTION',
}
_MP4_WRITE = {v: k for k, v in MP4_ATOMS.items()}
FREEFORM = '----:com.apple.iTunes:'

def _mp4_pairs(tags):
    out = []
    for key, vals in tags.items():
        if key == 'covr':
            continue
        if key in ('trkn', 'disk'):
            for n, tot in vals:
                out.append((key, f'{n}/{tot}' if tot else str(n)))
        elif key == 'cpil':
            out.append((key, '1' if vals else '0'))
        elif key.startswith('----:'):
            for v in vals:
                fmt = getattr(v, 'dataformat', 1)
                if fmt not in (1, 2):          # UTF-8 / UTF-16 text only; binary atoms (iTunes encoder data) stay untouched
                    continue
                try:
                    out.append((key, bytes(v).decode('utf-16-be' if fmt == 2 else 'utf-8')))
                except UnicodeDecodeError:
                    continue
        elif key in MP4_ATOMS and isinstance(vals, list):
            out += [(key, str(v)) for v in vals]
    return out   # other atoms (stik, rtng, purd, ...) are left alone

def mp4_canonical(key):
    if key in MP4_ATOMS:
        return MP4_ATOMS[key]
    if key.startswith('----:'):
        return key.split(':', 2)[2]
    return None

def _canonical_to_mp4(tagdict):
    t = dict(tagdict)
    out = []
    for atom, n_key, tot_key in (('trkn', 'TRACKNUMBER', 'TRACKTOTAL'), ('disk', 'DISCNUMBER', 'DISCTOTAL')):
        n, tot = t.pop(n_key, []), t.pop(tot_key, [])
        if n or tot:
            out.append((atom, f"{n[0] if n else 0}/{tot[0]}" if tot else n[0]))
    for k, vals in t.items():
        if k in _MP4_WRITE:
            key = _MP4_WRITE[k]
        elif k.startswith('----:') or k in MP4_ATOMS:
            key = k
        else:
            key = FREEFORM + TXXX_NAMES.get(k, k)
        out += [(key, v) for v in vals]
    return out

def _int(s):
    try:
        return int(str(s).strip().split('/')[0] or 0)
    except ValueError:
        return 0

def _write_mp4(audio, pairs):
    from mutagen.mp4 import MP4FreeForm, AtomDataType
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for key in list(tags.keys()):
        if key in MP4_ATOMS or key.startswith('----:'):
            del tags[key]
    grouped = {}
    for k, v in pairs:
        grouped.setdefault(k, []).append(v)
    for key, vals in grouped.items():
        if key in ('trkn', 'disk'):
            v = vals[0]
            n, _, tot = str(v).partition('/')
            tags[key] = [(_int(n), _int(tot))]
        elif key == 'cpil':
            tags[key] = str(vals[0]).strip() not in ('', '0', 'false', 'False')
        elif key == 'tmpo':
            tags[key] = [_int(v) for v in vals if _int(v)]
        elif key.startswith('----:'):
            tags[key] = [MP4FreeForm(v.encode('utf-8'), AtomDataType.UTF8) for v in vals]
        elif key in MP4_ATOMS:
            tags[key] = vals
        else:
            tags[FREEFORM + key] = [MP4FreeForm(v.encode('utf-8'), AtomDataType.UTF8) for v in vals]


# ── APEv2 (WavPack, Monkey's Audio, Musepack) ────────────────────────────────

APE_NAMES = {'ALBUMARTIST': 'Album Artist', 'DATE': 'Year', 'DISCSUBTITLE': 'DiscSubtitle',
             'TITLE': 'Title', 'ARTIST': 'Artist', 'ALBUM': 'Album', 'GENRE': 'Genre', 'COMPOSER': 'Composer',
             'COMMENT': 'Comment', 'LABEL': 'Label', 'CATALOGNUMBER': 'CatalogNumber', 'MEDIA': 'Media',
             'LYRICS': 'Lyrics', 'ISRC': 'ISRC', 'BARCODE': 'Barcode', 'LANGUAGE': 'Language',
             'COPYRIGHT': 'Copyright', 'CONDUCTOR': 'Conductor', 'LYRICIST': 'Lyricist'}

def _ape_pairs(tags):
    from mutagen.apev2 import APETextValue
    out = []
    for key, val in tags.items():
        if isinstance(val, APETextValue):
            out += [(key, v) for v in list(val)]
    return out

def _canonical_to_ape(tagdict):
    t = dict(tagdict)
    out = []
    for name, n_key, tot_key in (('Track', 'TRACKNUMBER', 'TRACKTOTAL'), ('Disc', 'DISCNUMBER', 'DISCTOTAL')):
        n, tot = t.pop(n_key, []), t.pop(tot_key, [])
        if n:
            out.append((name, f'{n[0]}/{tot[0]}' if tot else n[0]))
    for k, vals in t.items():
        out += [(APE_NAMES.get(k, k), v) for v in vals]
    return out

def _write_ape(audio, pairs):
    from mutagen.apev2 import APETextValue
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    for key in list(tags.keys()):
        if isinstance(tags[key], APETextValue):
            del tags[key]
    grouped = {}
    for k, v in pairs:
        grouped.setdefault(k.lower(), [k, []])[1].append(v)   # APE keys are case-insensitive
    for key, vals in grouped.values():
        tags[key] = vals


# ── ASF (WMA) ─────────────────────────────────────────────────────────────────

ASF_NAMES = {'Title': 'TITLE', 'Author': 'ARTIST', 'WM/AlbumTitle': 'ALBUM', 'WM/AlbumArtist': 'ALBUMARTIST',
             'WM/TrackNumber': 'TRACKNUMBER', 'WM/PartOfSet': 'DISCNUMBER', 'WM/Year': 'DATE',
             'WM/OriginalReleaseYear': 'ORIGINALDATE', 'WM/Genre': 'GENRE', 'WM/Composer': 'COMPOSER',
             'WM/Publisher': 'LABEL', 'WM/ISRC': 'ISRC', 'WM/Barcode': 'BARCODE', 'WM/CatalogNo': 'CATALOGNUMBER',
             'WM/Conductor': 'CONDUCTOR', 'WM/Writer': 'LYRICIST', 'WM/Media': 'MEDIA', 'Description': 'COMMENT',
             'WM/AlbumSortOrder': 'ALBUMSORT', 'WM/ArtistSortOrder': 'ARTISTSORT',
             'WM/AlbumArtistSortOrder': 'ALBUMARTISTSORT', 'WM/Lyrics': 'LYRICS', 'WM/Language': 'LANGUAGE',
             'WM/EncodedBy': 'ENCODEDBY', 'WM/SetSubTitle': 'DISCSUBTITLE', 'Copyright': 'COPYRIGHT'}
_ASF_WRITE = {v: k for k, v in ASF_NAMES.items()}

def _asf_pairs(tags):
    from mutagen.asf import ASFUnicodeAttribute, ASFDWordAttribute, ASFQWordAttribute, ASFWordAttribute, ASFBoolAttribute
    ok = (ASFUnicodeAttribute, ASFDWordAttribute, ASFQWordAttribute, ASFWordAttribute, ASFBoolAttribute)
    out = []
    for key, vals in tags.items():
        out += [(key, str(v.value)) for v in vals if isinstance(v, ok)]
    return out

def asf_canonical(key):
    if key in ASF_NAMES:
        return ASF_NAMES[key]
    if key.startswith('MusicBrainz/'):
        return 'MusicBrainz ' + key[12:]
    if key.startswith('WM/'):
        return key[3:]
    return None

def _canonical_to_asf(tagdict):
    t = dict(tagdict)
    t.pop('TRACKTOTAL', None); t.pop('DISCTOTAL', None)
    out = []
    for k, vals in t.items():
        if k in _ASF_WRITE:
            key = _ASF_WRITE[k]
        elif k.startswith('MUSICBRAINZ_') or k in TXXX_NAMES:
            name = TXXX_NAMES.get(k, k)
            key = 'MusicBrainz/' + name[12:] if name.startswith('MusicBrainz ') else name
        else:
            key = k
        out += [(key, v) for v in vals]
    return out

def _write_asf(audio, pairs):
    from mutagen.asf import ASFUnicodeAttribute, ASFDWordAttribute, ASFQWordAttribute, ASFWordAttribute, ASFBoolAttribute
    ok = (ASFUnicodeAttribute, ASFDWordAttribute, ASFQWordAttribute, ASFWordAttribute, ASFBoolAttribute)
    tags = audio.tags
    for key in list(tags.keys()):
        if all(isinstance(v, ok) for v in tags[key]):
            del tags[key]
    grouped = {}
    for k, v in pairs:
        grouped.setdefault(k, []).append(v)
    for key, vals in grouped.items():
        tags[key] = [ASFUnicodeAttribute(v) for v in vals]


# ── Ogg (Vorbis comments, pictures live in METADATA_BLOCK_PICTURE) ────────────

_OGG_PICTURE_KEYS = ('metadata_block_picture', 'coverart', 'coverartmime')

def _vorbis_pairs(tags):
    return [(k, v) for k, v in list(tags) if k.lower() not in _OGG_PICTURE_KEYS]

def _write_vorbis(audio, pairs):
    if audio.tags is None:
        audio.add_tags()
    keep = [(k, v) for k, v in list(audio.tags) if k.lower() in _OGG_PICTURE_KEYS]
    del audio.tags[:]
    for k, v in list(pairs) + keep:
        audio.tags.append((k, v))


# ── Public: read / write tags ─────────────────────────────────────────────────

def native_canonical(key):
    """Canonical-ish name for a format's own key (still to be passed through tags.canonical), or None."""
    if not key:
        return None
    return id3_canonical(key) or mp4_canonical(key) or asf_canonical(key)

def raw_pairs(audio, path):
    fam = family(path)
    tags = audio.tags
    if tags is None:
        return []
    if fam == 'id3':
        return _id3_pairs(tags)
    if fam == 'mp4':
        return _mp4_pairs(tags)
    if fam == 'ape':
        return _ape_pairs(tags)
    if fam == 'asf':
        return _asf_pairs(tags)
    return _vorbis_pairs(tags)

def read_raw(path):
    return raw_pairs(open_audio(path), path)

def to_native(path, tagdict):
    """Canonical {KEY: [values]} -> raw pairs under this format's field names."""
    fam = family(path)
    if fam == 'id3':
        return _canonical_to_id3(tagdict)
    if fam == 'mp4':
        return _canonical_to_mp4(tagdict)
    if fam == 'ape':
        return _canonical_to_ape(tagdict)
    if fam == 'asf':
        return _canonical_to_asf(tagdict)
    return [(k, v) for k, vals in tagdict.items() for v in vals]

def write(path, pairs):
    """Replace all text tags with these raw pairs (pictures and binary data stay)."""
    fam = family(path)
    with storage.open_file(path, 'rb+') as f:
        audio = load(f, path)
        if fam == 'id3':
            _write_id3(audio, pairs)
        elif fam == 'mp4':
            _write_mp4(audio, pairs)
        elif fam == 'ape':
            _write_ape(audio, pairs)
        elif fam == 'asf':
            _write_asf(audio, pairs)
        else:
            _write_vorbis(audio, pairs)
        f.seek(0)
        if fam == 'id3':
            audio.save(f, v2_version=_id3_version(audio))
        else:
            audio.save(f)


# ── Pictures ──────────────────────────────────────────────────────────────────

def _sniff(data):
    return ('image/jpeg' if data[:3] == b'\xff\xd8\xff' else 'image/png' if data[:4] == b'\x89PNG'
            else 'image/gif' if data[:3] == b'GIF' else 'image/webp' if data[:4] == b'RIFF' else None)

def pictures(audio, path):
    """[{'type', 'mime', 'data'}] embedded in a file of any format."""
    fam, tags = family(path), audio.tags
    out = []
    if fam == 'flac':
        return [{'type': p.type, 'mime': p.mime, 'data': p.data} for p in audio.pictures]
    if tags is None:
        return out
    if fam == 'id3':
        for fr in tags.getall('APIC'):
            out.append({'type': fr.type, 'mime': fr.mime, 'data': fr.data})
    elif fam == 'mp4':
        from mutagen.mp4 import MP4Cover
        for c in tags.get('covr', []):
            out.append({'type': 3, 'mime': 'image/png' if c.imageformat == MP4Cover.FORMAT_PNG else 'image/jpeg',
                        'data': bytes(c)})
    elif fam == 'ape':
        for key in tags.keys():
            if key.lower().startswith('cover art'):
                raw = bytes(tags[key].value)
                data = raw.split(b'\x00', 1)[1] if b'\x00' in raw[:256] else raw
                out.append({'type': 3 if 'front' in key.lower() else 0, 'mime': _sniff(data) or 'image/jpeg', 'data': data})
    elif fam == 'asf':
        from mutagen.asf import ASFByteArrayAttribute
        for v in tags.get('WM/Picture', []):
            raw = bytes(v.value) if isinstance(v, ASFByteArrayAttribute) else b''
            try:   # type(1) size(4) mime\0\0 desc\0\0 data   (UTF-16 strings)
                ptype = raw[0]
                p = 5
                end = raw.index(b'\x00\x00', p); end += (end - p) % 2
                mime = raw[p:end].decode('utf-16-le'); p = end + 2
                end = raw.index(b'\x00\x00', p); end += (end - p) % 2
                p = end + 2
                out.append({'type': ptype, 'mime': mime, 'data': raw[p:]})
            except (ValueError, IndexError):
                pass
    else:
        from mutagen.flac import Picture
        for k, v in list(tags):
            if k.lower() == 'metadata_block_picture':
                try:
                    p = Picture(base64.b64decode(v))
                    out.append({'type': p.type, 'mime': p.mime, 'data': p.data})
                except Exception:
                    pass
    return out

def can_embed(path):
    return family(path) in ('flac', 'vorbis', 'id3', 'mp4', 'ape')

def embed(path, data, mime):
    """Replace the embedded pictures of one file with this front cover."""
    fam = family(path)
    with storage.open_file(path, 'rb+') as f:
        audio = load(f, path)
        if fam == 'flac':
            from mutagen.flac import Picture
            pic = Picture(); pic.type = 3; pic.mime = mime; pic.data = data
            audio.clear_pictures(); audio.add_picture(pic)
        elif fam == 'id3':
            from mutagen.id3 import APIC
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall('APIC')
            audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc='', data=data))
        elif fam == 'mp4':
            from mutagen.mp4 import MP4Cover
            if audio.tags is None:
                audio.add_tags()
            audio.tags['covr'] = [MP4Cover(data, MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG)]
        elif fam == 'ape':
            from mutagen.apev2 import APEValue, BINARY
            if audio.tags is None:
                audio.add_tags()
            for key in [k for k in audio.tags.keys() if k.lower().startswith('cover art')]:
                del audio.tags[key]
            name = b'cover.png' if mime == 'image/png' else b'cover.jpg'
            audio.tags['Cover Art (Front)'] = APEValue(name + b'\x00' + data, BINARY)
        elif fam == 'vorbis':
            from mutagen.flac import Picture
            pic = Picture(); pic.type = 3; pic.mime = mime; pic.data = data
            if audio.tags is None:
                audio.add_tags()
            rest = [(k, v) for k, v in list(audio.tags) if k.lower() not in _OGG_PICTURE_KEYS]
            del audio.tags[:]
            for kv in rest:
                audio.tags.append(kv)
            audio.tags.append(('METADATA_BLOCK_PICTURE', base64.b64encode(pic.write()).decode('ascii')))
        else:
            raise ValueError(f'embedding art is not supported for {FORMAT_NAME.get(ext(path), ext(path))} files')
        f.seek(0)
        if fam == 'id3':
            audio.save(f, v2_version=_id3_version(audio))
        else:
            audio.save(f)


# ── Stream info ───────────────────────────────────────────────────────────────

def stream_info(audio, path):
    i = audio.info
    return {'sample_rate': getattr(i, 'sample_rate', 0) or 0,
            'bits': getattr(i, 'bits_per_sample', 0) or 0,
            'channels': getattr(i, 'channels', 0) or 0,
            'seconds': round(getattr(i, 'length', 0) or 0, 2),
            'bitrate': round((getattr(i, 'bitrate', 0) or 0) / 1000),
            'codec': (getattr(i, 'codec', '') or '').lower(),
            'format': FORMAT_NAME.get(ext(path), ext(path).lstrip('.').upper())}
