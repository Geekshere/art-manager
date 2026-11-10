"""
Fast health check for FLAC files and covers — without decoding audio.

Per file it reads only the metadata blocks (skipping over embedded pictures)
and a few KB at the start and end of the audio, so it is quick even over SMB
or WebDAV. It finds:

  error  not a FLAC file / damaged header / no audio
  error  truncated file: the last complete audio frame ends before the
         length the file claims, or the last frame fails its CRC-16
  error  the first audio frame is damaged
  warn   missing title / artist / album / track number tags
  warn   no embedded cover
  warn   ID3 tags in a FLAC file (some players refuse those files)
  info   very large embedded picture, several pictures, unknown length

and per album folder:

  error  cover.jpg unreadable
  warn   cover.jpg smaller than 500 px, or not really a JPEG
  warn   tracks disagree on the album name; duplicate / missing track numbers
  info   mixed sample rates or bit depths

Results are cached by (size, mtime) so a re-check only reads changed files.
A full decode (`flac -t`) is optional and only for local files.
"""
import io, os, shutil, struct, subprocess

from . import storage
from . import tags as tagmod

CHECK_VERSION = 2          # bump to invalidate cached results when checks change
MIN_COVER_PX = 500
BIG_PICTURE = 4 * 1024 * 1024
HEAD_READ = 64 * 1024
TAIL_READ = 64 * 1024
TAIL_READ_MAX = 512 * 1024


# ── CRCs used by FLAC frames ─────────────────────────────────────────────────

def _table(poly, width):
    top, mask, out = 1 << (width - 1), (1 << width) - 1, []
    for i in range(256):
        c = i << (width - 8)
        for _ in range(8):
            c = ((c << 1) ^ poly) if c & top else (c << 1)
        out.append(c & mask)
    return out

_CRC8 = _table(0x07, 8)
_CRC16 = _table(0x8005, 16)

def crc8(data):
    c = 0
    for b in data:
        c = _CRC8[c ^ b]
    return c

def crc16(data):
    c = 0
    t = _CRC16
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ t[(c >> 8) ^ b]
    return c


# ── Frame headers ─────────────────────────────────────────────────────────────

def parse_frame_header(buf, pos, streaminfo):
    """
    Parse a FLAC frame header at buf[pos]. Returns (end_sample, header_len) if
    it is a valid header (sync, reserved bits, CRC-8), else None. end_sample is
    the sample number just after this frame.
    """
    if pos + 6 > len(buf) or buf[pos] != 0xFF or buf[pos + 1] not in (0xF8, 0xF9):
        return None
    variable = buf[pos + 1] & 1
    b2, b3 = buf[pos + 2], buf[pos + 3]
    bs_code, sr_code = b2 >> 4, b2 & 0x0F
    ch, ss = b3 >> 4, (b3 >> 1) & 0x07
    if bs_code == 0 or sr_code == 0x0F or ch > 10 or b3 & 1 or ss == 3:
        return None
    # frame/sample number, coded like UTF-8 (up to 7 bytes)
    i = pos + 4
    first = buf[i]
    ones = 0
    while ones < 8 and first & (0x80 >> ones):
        ones += 1
    if ones == 0:
        n, extra = first, 0
    elif ones == 1 or ones == 8:
        return None                        # continuation byte / 0xFF: not a valid start
    else:
        extra = ones - 1                   # 110xxxxx -> 1 more byte ... 11111110 -> 6 more
        n = first & (0x7F >> ones)
        if i + 1 + extra > len(buf):
            return None
        for k in range(1, extra + 1):
            cb = buf[i + k]
            if cb & 0xC0 != 0x80:
                return None
            n = (n << 6) | (cb & 0x3F)
    i += 1 + extra
    if bs_code == 1:
        bs = 192
    elif 2 <= bs_code <= 5:
        bs = 576 << (bs_code - 2)
    elif bs_code == 6:
        if i + 1 > len(buf): return None
        bs = buf[i] + 1; i += 1
    elif bs_code == 7:
        if i + 2 > len(buf): return None
        bs = (buf[i] << 8 | buf[i + 1]) + 1; i += 2
    else:
        bs = 256 << (bs_code - 8)
    if sr_code == 12:
        i += 1
    elif sr_code in (13, 14):
        i += 2
    if i + 1 > len(buf) or crc8(buf[pos:i]) != buf[i]:
        return None
    if variable:
        end = n + bs
    else:
        nominal = (streaminfo or {}).get('max_blocksize') or bs
        end = n * nominal + bs
    return end, i + 1 - pos


# ── File parsing ──────────────────────────────────────────────────────────────

class _Reader:
    """Random access with one up-front read of the head of the file."""

    def __init__(self, f, size):
        self.f, self.size = f, size
        self.head = f.read(min(HEAD_READ, size))

    def read_at(self, off, n):
        if off + n <= len(self.head):
            return self.head[off:off + n]
        self.f.seek(off)
        return self.f.read(n)


def _issue(level, code, msg):
    return {'level': level, 'code': code, 'msg': msg}

def _fmt_time(samples, rate):
    if not rate:
        return '?'
    s = int(samples / rate)
    return f'{s // 60}:{s % 60:02d}'


def check_flac_stream(f, size):
    """Check an open FLAC file. Returns {'issues': [...], 'info': {...}}."""
    issues, info = [], {}
    if size < 42:
        return {'issues': [_issue('error', 'not_flac', 'File is too small to be a FLAC file')], 'info': info}
    r = _Reader(f, size)
    off = 0
    if r.head[:3] == b'ID3':
        tag_size = 10
        h = r.head[6:10]
        if len(h) == 4:
            tag_size += (h[0] << 21) | (h[1] << 14) | (h[2] << 7) | h[3]
        issues.append(_issue('warn', 'id3', 'ID3 tag in front of the FLAC data (some players refuse such files)'))
        off = tag_size
    if r.read_at(off, 4) != b'fLaC':
        issues.append(_issue('error', 'not_flac', 'Not a FLAC file (no "fLaC" signature)'))
        return {'issues': issues, 'info': info}
    off += 4

    streaminfo, tags, pictures = None, {}, []
    raw_tags, tag_blocks = [], 0
    last = False
    while not last:
        hdr = r.read_at(off, 4)
        if len(hdr) < 4:
            issues.append(_issue('error', 'bad_metadata', 'File ends inside the metadata'))
            return {'issues': issues, 'info': info}
        last, btype = bool(hdr[0] & 0x80), hdr[0] & 0x7F
        blen = int.from_bytes(hdr[1:4], 'big')
        body_off = off + 4
        if body_off + blen > size:
            issues.append(_issue('error', 'bad_metadata', 'A metadata block runs past the end of the file'))
            return {'issues': issues, 'info': info}
        if btype == 0:
            d = r.read_at(body_off, 34)
            if blen != 34 or len(d) != 34:
                issues.append(_issue('error', 'bad_metadata', 'Damaged STREAMINFO block'))
                return {'issues': issues, 'info': info}
            min_bs, max_bs = struct.unpack('>HH', d[:4])
            max_fs = int.from_bytes(d[7:10], 'big')
            x = int.from_bytes(d[10:18], 'big')
            streaminfo = {
                'min_blocksize': min_bs, 'max_blocksize': max_bs, 'max_framesize': max_fs,
                'sample_rate': x >> 44, 'channels': ((x >> 41) & 0x7) + 1,
                'bits': ((x >> 36) & 0x1F) + 1, 'total_samples': x & 0xFFFFFFFFF,
            }
        elif btype == 4:
            d = r.read_at(body_off, min(blen, 1024 * 1024))
            try:
                raw_tags = tagmod.parse_vorbis_comment(d)
                for k, v in raw_tags:
                    tags.setdefault(tagmod.canonical(k), v)
            except struct.error:
                issues.append(_issue('warn', 'bad_tags', 'The tag block is damaged'))
            if tag_blocks:
                issues.append(_issue('warn', 'two_tag_blocks', 'The file has more than one tag block (players read only one)'))
            tag_blocks += 1
        elif btype == 6:
            d = r.read_at(body_off, min(blen, 4096))
            try:
                ptype, mlen = struct.unpack_from('>II', d, 0)
                mime = d[8:8 + mlen].decode('ascii', 'replace')
                q = 8 + mlen
                dlen = struct.unpack_from('>I', d, q)[0]; q += 4 + dlen
                width, height = struct.unpack_from('>II', d, q); q += 16
                q += 4                                     # picture data length
                magic = d[q:q + 4]
                real = ('image/jpeg' if magic[:3] == b'\xff\xd8\xff' else 'image/png' if magic == b'\x89PNG'
                        else 'image/gif' if magic[:3] == b'GIF' else 'image/webp' if magic == b'RIFF' else None)
                pictures.append({'type': ptype, 'mime': mime, 'size': blen, 'real': real,
                                 'width': width, 'height': height})
            except struct.error:
                issues.append(_issue('warn', 'bad_picture', 'An embedded picture block is damaged'))
        off = body_off + blen
    audio_off = off

    if streaminfo is None:
        issues.append(_issue('error', 'bad_metadata', 'No STREAMINFO block'))
        return {'issues': issues, 'info': info}
    si = streaminfo
    info.update(sample_rate=si['sample_rate'], bits=si['bits'], channels=si['channels'],
                seconds=round(si['total_samples'] / si['sample_rate'], 2) if si['sample_rate'] else None,
                pictures=len(pictures), tags={k: tags[k] for k in
                                              ('TITLE', 'ARTIST', 'ALBUM', 'ALBUMARTIST', 'TRACKNUMBER', 'TRACKTOTAL',
                                               'DISCNUMBER', 'DATE') if k in tags})

    audio_len = size - audio_off
    if audio_len < 16:
        issues.append(_issue('error', 'no_audio', 'No audio data after the metadata'))
        return {'issues': issues, 'info': info}

    # first frame
    first = r.read_at(audio_off, 32)
    if parse_frame_header(first, 0, si) is None:
        issues.append(_issue('error', 'bad_first_frame', 'The audio data does not start with a valid frame'))

    # last frame: find the last frame header whose CRC-16 checks out to the end of the file
    tail_issue = _check_tail(r, audio_off, size, si, issues)
    if tail_issue:
        issues.append(tail_issue)

    # tags and art
    missing = [k.lower() for k in ('TITLE', 'ARTIST', 'ALBUM', 'TRACKNUMBER') if not tags.get(k, '').strip()]
    if missing:
        issues.append(_issue('warn', 'missing_tags', 'Missing tags: ' + ', '.join(missing)))
    if not pictures:
        issues.append(_issue('warn', 'no_art', 'No embedded cover'))
    elif len(pictures) > 1:
        issues.append(_issue('info', 'many_pictures', f'{len(pictures)} embedded pictures'))
    big = [p for p in pictures if p['size'] > BIG_PICTURE]
    if big:
        issues.append(_issue('info', 'big_picture',
                             f'Embedded picture is {big[0]["size"] // (1024 * 1024)} MB (makes every track larger)'))
    if not si['total_samples']:
        issues.append(_issue('info', 'unknown_length', 'The file does not record its length'))

    # odd tags: the same field under several names, duplicates, garbled text, ...
    for level, code, msg in tagmod.tag_problems(raw_tags):
        issues.append(_issue(level, code, msg))
    # pictures that some players won't use
    for p in pictures:
        if p.get('real') and p['mime'].lower().replace('jpg', 'jpeg') != p['real']:
            issues.append(_issue('warn', 'picture_mime', f'Embedded picture says {p["mime"] or "nothing"} but is really {p["real"]}'))
            break
    if pictures and not any(p['type'] == 3 for p in pictures):
        issues.append(_issue('info', 'picture_type', "The embedded picture isn't marked as the front cover (some players skip it)"))
    small = [p for p in pictures if p['type'] == 3 and 0 < min(p['width'], p['height']) < 300]
    if small:
        issues.append(_issue('info', 'small_picture', f'Embedded cover is only {small[0]["width"]}×{small[0]["height"]} px'))
    # unusual audio formats
    if si['channels'] == 1:
        issues.append(_issue('info', 'mono', 'Mono recording'))
    elif si['channels'] > 2:
        issues.append(_issue('info', 'multichannel', f'{si["channels"]} channels (multichannel)'))
    if si['sample_rate'] and si['sample_rate'] < 44100:
        issues.append(_issue('info', 'low_rate', f'Low sample rate: {si["sample_rate"] / 1000:g} kHz'))
    if si['bits'] not in (16, 24):
        issues.append(_issue('info', 'odd_bits', f'Unusual bit depth: {si["bits"]}-bit'))
    return {'issues': issues, 'info': info}


def _check_tail(r, audio_off, size, si, issues):
    """
    Find the last audio frame and verify it with its CRC-16. A frame has no
    length field, so the last frame must run exactly to the end of the file:
    a file cut short (or with a damaged end) fails that check. Cost: one small
    read and one CRC over the last frame or two.
    """
    total, rate = si['total_samples'], si['sample_rate']
    tail_len = max(16 * 1024, si['max_framesize'] * 2 + 64) if si['max_framesize'] else TAIL_READ
    while True:
        n = min(tail_len, size - audio_off)
        buf = r.read_at(size - n, n)
        # tags some tools append after the audio
        if len(buf) >= 128 and buf[-128:-125] == b'TAG':
            buf = buf[:-128]
            issues.append(_issue('warn', 'id3v1', 'ID3v1 tag at the end of the FLAC file'))
        if len(buf) >= 32 and buf[-32:-24] == b'APETAGEX':
            ape_size = int.from_bytes(buf[-20:-16], 'little') + 32
            buf = buf[:max(0, len(buf) - ape_size)]
            issues.append(_issue('warn', 'ape', 'APE tag at the end of the FLAC file'))

        # valid frame headers (sync + CRC-8), last ones first
        headers, pos = [], len(buf)
        while len(headers) < 3:
            pos = max(buf.rfind(b'\xff\xf8', 0, pos), buf.rfind(b'\xff\xf9', 0, pos))
            if pos < 0:
                break
            h = parse_frame_header(buf, pos, si)
            if h:
                headers.append((pos, h[0]))
        if headers or n >= size - audio_off or tail_len >= TAIL_READ_MAX:
            break
        tail_len = TAIL_READ_MAX          # frames bigger than expected: look further back once

    if not headers:
        return _issue('error', 'damaged_end', 'No complete audio frame found at the end of the file')
    for pos, end_sample in headers:       # normally the very first one is the real last frame
        frame = buf[pos:]
        if len(frame) > 2 and crc16(frame[:-2]) == int.from_bytes(frame[-2:], 'big'):
            if total and end_sample < total:
                return _issue('error', 'truncated', f'File is cut off: audio stops at '
                              f'{_fmt_time(end_sample, rate)} of {_fmt_time(total, rate)}')
            return None
    end_sample = headers[0][1]
    if total and end_sample < total:
        return _issue('error', 'truncated', f'File is cut off: audio stops at about '
                      f'{_fmt_time(end_sample, rate)} of {_fmt_time(total, rate)}')
    return _issue('error', 'damaged_end', 'The end of the file is damaged (last audio frame is incomplete)')


# ── Per file / per album ──────────────────────────────────────────────────────

def check_flac(path, size=None):
    try:
        if size is None:
            size = storage.stat(path).size
        with storage.open_file(path, 'rb') as f:
            return check_flac_stream(f, size)
    except Exception as e:
        return {'issues': [_issue('error', 'unreadable', f'Could not read the file: {storage.error_message(e, path)}')],
                'info': {}}


def deep_check_available():
    return shutil.which('flac') is not None

def deep_check(path):
    """Decode the whole file with the reference `flac` tool (local files only)."""
    if storage.kind(path) != 'local' or not deep_check_available():
        return None
    try:
        p = subprocess.run(['flac', '-t', '-s', path], capture_output=True, text=True, timeout=600)
    except Exception as e:
        return _issue('error', 'decode', f'Full decode failed to run: {e}')
    if p.returncode != 0:
        lines = [l.strip() for l in (p.stderr or p.stdout).splitlines() if l.strip()]
        text = ' '.join(lines)
        for key, plain in (('LOST_SYNC', 'the audio data is damaged or cut off'),
                           ('FRAME_CRC_MISMATCH', 'the audio data is corrupted (checksum mismatch)'),
                           ('BAD_HEADER', 'an audio frame header is damaged'),
                           ('MD5 signature mismatch', "the decoded audio doesn't match the file's own checksum"),
                           ('UNPARSEABLE_STREAM', 'the audio stream cannot be decoded')):
            if key in text:
                return _issue('error', 'decode', f'Full decode: {plain} ({key})')
        return _issue('error', 'decode', 'Full decode failed: ' + (lines[0] if lines else 'unknown error'))
    return None


def check_cover(path):
    """Issues for one cover.jpg (reads only the image header)."""
    from PIL import Image
    try:
        with storage.open_file(path, 'rb') as f:
            im = Image.open(f)
            w, h = im.size
            fmt = im.format
    except Exception:
        return [_issue('error', 'bad_cover', f'{storage.basename(path)} is damaged or not an image')]
    out = []
    if fmt != 'JPEG':
        out.append(_issue('warn', 'cover_not_jpeg', f'{storage.basename(path)} is really a {fmt} file'))
    if min(w, h) < MIN_COVER_PX:
        out.append(_issue('warn', 'small_cover', f'{storage.basename(path)} is only {w}×{h} px'))
    return out


def album_issues(file_results, album_name, extras=None, multi_disc=False):
    """Consistency checks across the tracks of one album."""
    out = []
    infos = [r['info'] for r in file_results if r.get('info')]
    extras = extras or {}
    # album artist: different values split the album in players
    aas = [i.get('tags', {}).get('ALBUMARTIST', '').strip() for i in infos]
    distinct = sorted({a for a in aas if a})
    if len(distinct) > 1:
        out.append(_issue('warn', 'albumartist_mismatch', 'Tracks have different album artists (players may split '
                          'the album): ' + ', '.join(f'"{a}"' for a in distinct)[:200]))
    elif distinct and '' in aas:
        out.append(_issue('warn', 'albumartist_missing', f'{aas.count("")} track(s) have no album artist while '
                          'the others do (players may split the album)'))
    dates = sorted({i.get('tags', {}).get('DATE', '').strip()[:4] for i in infos} - {''})
    if len(dates) > 1:
        out.append(_issue('info', 'date_mismatch', 'Tracks have different years: ' + ', '.join(dates)))
    if multi_disc and infos and any(not i.get('tags', {}).get('DISCNUMBER') for i in infos):
        out.append(_issue('warn', 'no_discnumber', 'Multi-disc album, but some tracks have no disc number '
                          '(players may mix the discs up)'))
    totals = {i.get('tags', {}).get('TRACKTOTAL', '').strip() for i in infos} - {''}
    if not multi_disc and len(totals) == 1 and totals != {str(len(infos))} and next(iter(totals)).isdigit():
        out.append(_issue('info', 'tracktotal', f'Track total says {next(iter(totals))}, the folder has {len(infos)} tracks'))
    if extras.get('junk'):
        out.append(_issue('info', 'junk_files', 'Leftover files: ' + ', '.join(extras['junk'][:8])
                          + (' …' if len(extras['junk']) > 8 else '')))
    if extras.get('other_audio'):
        kinds = sorted({n.rsplit('.', 1)[-1].upper() for n in extras['other_audio']})
        out.append(_issue('warn', 'other_audio', f'Also contains {len(extras["other_audio"])} non-FLAC audio '
                          f'file(s) ({", ".join(kinds)}) — players may show duplicate tracks'))
    albums = {i['tags'].get('ALBUM', '').strip() for i in infos if i.get('tags', {}).get('ALBUM')}
    if len(albums) > 1:
        out.append(_issue('warn', 'album_mismatch', 'Tracks disagree on the album name: ' +
                          ', '.join(sorted(albums))[:200]))
    rates = {(i.get('sample_rate'), i.get('bits')) for i in infos if i.get('sample_rate')}
    if len(rates) > 1:
        out.append(_issue('info', 'mixed_format', 'Mixed formats: ' +
                          ', '.join(f'{b}-bit/{r / 1000:g} kHz' for r, b in sorted(rates))))
    by_disc = {}
    for i in infos:
        t = i.get('tags', {})
        tn = t.get('TRACKNUMBER', '').split('/')[0].strip()
        if tn.isdigit():
            by_disc.setdefault(t.get('DISCNUMBER', '1').split('/')[0].strip() or '1', []).append(int(tn))
    for disc, nums in by_disc.items():
        dupes = sorted({n for n in nums if nums.count(n) > 1})
        label = f' (disc {disc})' if len(by_disc) > 1 else ''
        if dupes:
            out.append(_issue('warn', 'dup_tracks', f'Duplicate track numbers{label}: ' +
                              ', '.join(map(str, dupes))))
        missing = sorted(set(range(1, max(nums) + 1)) - set(nums)) if nums else []
        if missing and len(missing) <= 20:
            out.append(_issue('info', 'gap_tracks', f'Missing track numbers{label}: ' +
                              ', '.join(map(str, missing))))
    return out


# ── Library / album runs ──────────────────────────────────────────────────────

import concurrent.futures, json, threading, time, uuid

from . import config
from .scan import find_cover, is_track, list_dir

_cache_lock = threading.Lock()
_cache = None          # {path: {'size', 'fp', 'result'}}


def _cache_file():
    return os.path.join(config.DATA_DIR, 'check_cache.json')

def report_file():
    return os.path.join(config.DATA_DIR, 'check_report.json')

def _load_cache():
    global _cache
    with _cache_lock:
        if _cache is None:
            try:
                with open(_cache_file(), encoding='utf-8') as f:
                    data = json.load(f)
                _cache = data['entries'] if data.get('version') == CHECK_VERSION else {}
            except Exception:
                _cache = {}
        return _cache

def _save_cache():
    with _cache_lock:
        try:
            tmp = _cache_file() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'version': CHECK_VERSION, 'entries': _cache}, f)
            os.replace(tmp, _cache_file())
        except Exception:
            pass


_JUNK = ('thumbs.db', 'desktop.ini', '.ds_store')
_JUNK_EXT = ('.part', '.tmp', '.crdownload', '.!qb', '.bak')
_OTHER_AUDIO = ('.mp3', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.aif', '.aiff', '.ape', '.wv', '.wma', '.alac', '.dsf')

def _album_tasks(album):
    """
    -> ([(kind, path, Entry)] for every FLAC and every required cover file,
        {'junk': [...], 'other_audio': [...]} names found next to them)
    """
    tasks, extras = [], {'junk': [], 'other_audio': []}
    folders = [album['path']] + [d['path'] for d in album.get('disc_info', []) if d['path'] != album['path']]
    for folder in folders:
        files, _ = list_dir(folder)
        prefix = '' if folder == album['path'] else storage.basename(folder) + '/'
        for name, e in sorted(files.items()):
            low = name.lower()
            if is_track(name):
                tasks.append(('flac', storage.join(folder, name), e))
            elif low.startswith('._') or low in _JUNK or low.endswith(_JUNK_EXT):
                extras['junk'].append(prefix + name)
            elif low.endswith(_OTHER_AUDIO):
                extras['other_audio'].append(prefix + name)
        cov = find_cover(folder, files)
        if cov:
            tasks.append(('cover', cov, files[storage.basename(cov)]))
    return tasks, extras


def _run_task(kind, path, entry, deep):
    cache = _load_cache()
    key = ('deep:' if deep and kind == 'flac' else '') + path
    fp = entry.fingerprint if entry is not None else None
    hit = cache.get(key)
    if hit and fp and hit.get('fp') == fp and hit.get('size') == entry.size:
        return hit['result']
    if kind == 'cover':
        result = {'issues': check_cover(path), 'info': {}}
    else:
        result = check_flac(path, entry.size if entry is not None else None)
        if deep and not any(i['level'] == 'error' for i in result['issues']):
            d = deep_check(path)
            if d:
                result['issues'].append(d)
    if fp:
        with _cache_lock:
            cache[key] = {'size': entry.size, 'fp': fp, 'result': result}
    return result


def check_album(album, deep=False, pool=None):
    """Check one album -> {'issues': [...album-level...], 'files': [{name, path, kind, issues, info}]}."""
    try:
        tasks, extras = _album_tasks(album)
    except Exception as e:
        return {'issues': [_issue('error', 'unreadable', storage.error_message(e, album['path']))], 'files': []}
    own = pool is None
    pool = pool or concurrent.futures.ThreadPoolExecutor(max_workers=storage.parallelism(album['path']))
    try:
        results = list(pool.map(lambda t: _run_task(*t, deep), tasks))
    finally:
        if own:
            pool.shutdown()
    base = album['path'].rstrip('/\\')
    def rel(p):   # 'CD 2/01.flac' for multi-disc albums, plain file name otherwise
        return p[len(base) + 1:].replace('\\', '/') if p.startswith(base) and len(p) > len(base) else storage.basename(p)
    files = [{'name': rel(p), 'path': p, 'kind': k, 'issues': r['issues'], 'info': r.get('info', {})}
             for (k, p, _), r in zip(tasks, results)]
    flacs = [f for f in files if f['kind'] == 'flac']
    multi = any(d.get('path') != album['path'] for d in album.get('disc_info', []))
    return {'issues': album_issues(flacs, album['name'], extras, multi), 'files': files}


class CheckJob:
    def __init__(self, lib, deep):
        self.id = uuid.uuid4().hex[:12]
        self.lib, self.deep = lib, deep
        self.albums = [(a['name'], al) for a in lib.get('artists', []) for al in a.get('albums', [])]
        self.total, self.done, self.current = len(self.albums), 0, ''
        self.status, self.cancelled = 'running', False
        self.report = None
        self.started = time.time()

    def progress(self):
        out = {'id': self.id, 'status': self.status, 'done': self.done, 'total': self.total,
               'current': self.current, 'elapsed': round(time.time() - self.started, 1)}
        if self.report:
            out['report'] = self.report
        return out

    def run(self):
        found = []
        counts = {'error': 0, 'warn': 0, 'info': 0}
        files_checked = 0
        # 1 worker for local disks (fastest), 8 for network shares (latency-bound)
        workers = storage.parallelism(self.lib.get('root') or '')
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for artist, al in self.albums:
                if self.cancelled:
                    break
                self.current = f'{artist} — {al["name"]}'
                res = check_album(al, self.deep, pool)
                files_checked += sum(1 for f in res['files'] if f['kind'] == 'flac')
                bad_files = [f for f in res['files'] if f['issues']]
                for i in res['issues'] + [i for f in bad_files for i in f['issues']]:
                    counts[i['level']] += 1
                if res['issues'] or bad_files:
                    found.append({'artist': artist, 'album': al['name'], 'path': al['path'],
                                  'issues': res['issues'],
                                  'files': [{k: f[k] for k in ('name', 'path', 'kind', 'issues')} for f in bad_files]})
                self.done += 1
        _save_cache()
        self.report = {'root': self.lib.get('root'), 'finished_at': time.time(), 'deep': self.deep,
                       'complete': not self.cancelled, 'albums_checked': self.done,
                       'files_checked': files_checked, 'seconds': round(time.time() - self.started, 1),
                       'counts': counts, 'albums': found}
        try:
            with open(report_file(), 'w', encoding='utf-8') as f:
                json.dump(self.report, f)
        except Exception:
            pass
        self.status = 'cancelled' if self.cancelled else 'done'


_jobs = {}
_jobs_lock = threading.Lock()

def start_job(lib, deep=False):
    with _jobs_lock:
        for j in _jobs.values():
            if j.status == 'running':
                return j      # one library check at a time
        job = CheckJob(lib, deep)
        _jobs[job.id] = job
    threading.Thread(target=job.run, daemon=True, name='check').start()
    return job

def get_job(job_id):
    return _jobs.get(job_id)

def running_job():
    return next((j for j in _jobs.values() if j.status == 'running'), None)

def last_report():
    try:
        with open(report_file(), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None
