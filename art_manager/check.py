"""
Fast health check for audio files (FLAC in depth; MP3, M4A, Ogg, Opus, WAV, AIFF, ... too) and covers — without decoding audio.

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

from . import formats, storage
from . import tags as tagmod

CHECK_VERSION = 5          # bump to invalidate cached results when checks change
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

    if not si['total_samples']:
        issues.append(_issue('info', 'unknown_length', 'The file does not record its length'))
    _common_checks(issues, tags, raw_tags, pictures, si, lossless=True)
    return {'issues': issues, 'info': info}


def _common_checks(issues, tags, raw_tags, pictures, si, lossless):
    """Checks shared by every format: tags, embedded pictures, unusual audio."""
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
    if si.get('channels') == 1:
        issues.append(_issue('info', 'mono', 'Mono recording'))
    elif (si.get('channels') or 0) > 2:
        issues.append(_issue('info', 'multichannel', f'{si["channels"]} channels (multichannel)'))
    if si.get('sample_rate') and si['sample_rate'] < 44100:
        issues.append(_issue('info', 'low_rate', f'Low sample rate: {si["sample_rate"] / 1000:g} kHz'))
    if lossless and si['bits'] and si['bits'] not in (16, 24):
        issues.append(_issue('info', 'odd_bits', f'Unusual bit depth: {si["bits"]}-bit'))


def _check_tail(r, audio_off, size, si, issues):
    """
    Find the last audio frame and verify it with its CRC-16. A frame has no
    length field, so the last frame must run exactly to the end of the file:
    a file cut short (or with a damaged end) fails that check. Cost: one small
    read and one CRC over the last frame or two; only when that fails, a
    second look with a 512 KB window (STREAMINFO's maximum frame size is
    sometimes wrong) before anything is reported.
    """
    total, rate = si['total_samples'], si['sample_rate']
    tail_len = max(16 * 1024, si['max_framesize'] * 2 + 64) if si['max_framesize'] else TAIL_READ
    tags_noted = set()
    while True:
        n = min(tail_len, size - audio_off)
        buf = r.read_at(size - n, n)
        # tags some tools append after the audio
        if len(buf) >= 128 and buf[-128:-125] == b'TAG':
            buf = buf[:-128]
            tags_noted.add('id3v1')
        if len(buf) >= 32 and buf[-32:-24] == b'APETAGEX':
            ape_size = int.from_bytes(buf[-20:-16], 'little') + 32
            buf = buf[:max(0, len(buf) - ape_size)]
            tags_noted.add('ape')
        verdict, padded = _last_frame(buf, si, total, rate)
        if verdict is None or n >= size - audio_off or tail_len >= TAIL_READ_MAX:
            break
        tail_len = TAIL_READ_MAX          # look further back once before reporting anything
    if 'id3v1' in tags_noted:
        issues.append(_issue('warn', 'id3v1', 'ID3v1 tag at the end of the FLAC file'))
    if 'ape' in tags_noted:
        issues.append(_issue('warn', 'ape', 'APE tag at the end of the FLAC file'))
    if verdict is None and padded:
        issues.append(_issue('info', 'trailing_zeros', f'{padded:,} zero bytes after the last audio frame (harmless)'))
    if verdict is not None and verdict['code'] == 'damaged_end':
        # Before calling a file damaged: walk every frame of the last 2 MB and
        # check each one's CRC-16 (only for the rare file that failed above).
        n = min(CONFIRM_READ, size - audio_off)
        buf = r.read_at(size - n, n)
        if len(buf) >= 128 and buf[-128:-125] == b'TAG':
            buf = buf[:-128]
        if len(buf) >= 32 and buf[-32:-24] == b'APETAGEX':
            buf = buf[:max(0, len(buf) - int.from_bytes(buf[-20:-16], 'little') - 32)]
        ok, where = walk_frames(buf.rstrip(b'\0'), si)
        if ok:
            return None
        if where is not None:
            verdict = _issue('error', 'damaged_end', f'The audio is damaged near the end (a frame at about '
                             f'{_fmt_time(where, rate)} fails its checksum)')
    return verdict

CONFIRM_READ = 2 * 1024 * 1024

def walk_frames(buf, si):
    """
    Verify every frame in buf, whose end is the end of the audio: start at the
    first header that begins a chain of CRC-16-valid frames, then follow frame
    after frame to the end. -> (True, None) if every frame checks out to the
    end; (False, sample number of the first bad frame) otherwise; (False, None)
    if no valid frame was found at all.
    """
    cand = []
    pos = 0
    while True:
        a, b = buf.find(b'\xff\xf8', pos), buf.find(b'\xff\xf9', pos)
        q = min(x for x in (a, b, len(buf)) if x >= 0)
        if q >= len(buf):
            break
        h = parse_frame_header(buf, q, si)
        if h:
            cand.append((q, h[1], h[0]))
        pos = q + 1
    starts = set(c[0] for c in cand)
    t = _CRC16

    def frame_end(p):
        """End of the CRC-valid frame starting at p (next header or end of buf), or None."""
        c = 0
        n = len(buf)
        for j in range(p, n - 2):
            c = ((c << 8) & 0xFFFF) ^ t[(c >> 8) ^ buf[j]]
            k = j + 3          # a frame [p, k) whose last 2 bytes are buf[j+1:j+3]
            if (k in starts or k == n) and c == (buf[j + 1] << 8 | buf[j + 2]):
                return k
        return None

    i = 0
    while i < len(cand) and i < 64:     # find a real frame to start from
        p = cand[i][0]
        e = frame_end(p)
        if e is not None and (e == len(buf) or frame_end(e) is not None):
            break
        i += 1
    else:
        return False, None
    by_pos = {c[0]: c for c in cand}
    while True:
        e = frame_end(p)
        if e is None:
            return False, by_pos[p][2] - 1 if p in by_pos else None
        if e == len(buf):
            return True, None
        p = e

def _last_frame(buf, si, total, rate):
    """
    -> (issue or None, zero bytes skipped). Tries the end of the buffer as is,
    then without trailing zero padding (some tools pad files).
    """
    stripped = buf.rstrip(b'\0')
    pad = len(buf) - len(stripped)
    attempts = [(buf, 0)] + ([(stripped, pad)] if pad else [])
    first_issue = None
    for b, padded in attempts:
        headers, pos = [], len(b)
        while len(headers) < 8:           # valid frame headers (sync + CRC-8), last ones first
            pos = max(b.rfind(b'\xff\xf8', 0, pos), b.rfind(b'\xff\xf9', 0, pos))
            if pos < 0:
                break
            h = parse_frame_header(b, pos, si)
            if h:
                headers.append((pos, h[0]))
        if not headers:
            first_issue = first_issue or _issue('error', 'damaged_end', 'No complete audio frame found at the end of the file')
            continue
        for pos, end_sample in headers:   # normally the very first one is the real last frame
            frame = b[pos:]
            if len(frame) > 2 and crc16(frame[:-2]) == int.from_bytes(frame[-2:], 'big'):
                if total and end_sample < total:
                    return _issue('error', 'truncated', f'File is cut off: audio stops at '
                                  f'{_fmt_time(end_sample, rate)} of {_fmt_time(total, rate)}'), padded
                return None, padded
        end_sample = headers[0][1]
        if total and end_sample < total:
            first_issue = first_issue or _issue('error', 'truncated', f'File is cut off: audio stops at about '
                                                f'{_fmt_time(end_sample, rate)} of {_fmt_time(total, rate)}')
        else:
            first_issue = first_issue or _issue(
                'error', 'damaged_end', 'The end of the file is damaged (last audio frame fails its checksum) — '
                'confirm with "also decode every file completely" or `flac -t`')
    return first_issue, 0

# ── Other formats ─────────────────────────────────────────────────────────────

_MPEG_BR = {   # (version is MPEG-1, layer) -> kbps by index
    (True, 1): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (True, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (True, 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (False, 1): [0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256],
    (False, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MPEG_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}

def mpeg_frame_len(buf, p):
    """Length of the MP3 (MPEG audio) frame whose header is at buf[p], or None if it isn't one."""
    if p + 4 > len(buf) or buf[p] != 0xFF or buf[p + 1] & 0xE0 != 0xE0:
        return None
    ver, layer_bits = (buf[p + 1] >> 3) & 3, (buf[p + 1] >> 1) & 3
    br_i, sr_i, pad = buf[p + 2] >> 4, (buf[p + 2] >> 2) & 3, (buf[p + 2] >> 1) & 1
    if ver == 1 or layer_bits == 0 or br_i in (0, 15) or sr_i == 3:
        return None
    layer = 4 - layer_bits
    v1 = ver == 3
    br = _MPEG_BR[(v1, layer)] if v1 else _MPEG_BR[(False, 1 if layer == 1 else 2)]
    kbps, sr = br[br_i], _MPEG_SR[ver][sr_i]
    if layer == 1:
        return (12 * kbps * 1000 // sr + pad) * 4
    if layer == 3 and not v1:
        return 72 * kbps * 1000 // sr + pad
    return 144 * kbps * 1000 // sr + pad

def _strip_end_tags(buf):
    """Remove ID3v1 / APEv2 / Lyrics3 tags from the end of a tail buffer."""
    for _ in range(3):
        if len(buf) >= 128 and buf[-128:-125] == b'TAG':
            buf = buf[:-128]
        elif len(buf) >= 32 and buf[-32:-24] == b'APETAGEX':
            ape = int.from_bytes(buf[-20:-16], 'little') + 32
            buf = buf[:max(0, len(buf) - ape)]
        elif len(buf) >= 15 and buf[-9:] == b'LYRICS200':
            try:
                buf = buf[:max(0, len(buf) - 15 - int(buf[-15:-9]))]
            except ValueError:
                break
        else:
            break
    return buf

def _mp3_end(f, size):
    """An issue if the last MP3 frame is cut off (a file stopped downloading/copying part-way)."""
    n = min(size, 16 * 1024)
    f.seek(size - n)
    buf = _strip_end_tags(f.read(n))
    # find a run of 3 frames that follow each other, then walk to the end
    for p in range(0, max(0, len(buf) - 4)):
        l1 = mpeg_frame_len(buf, p)
        if not l1:
            continue
        l2 = mpeg_frame_len(buf, p + l1)
        if not l2 or not mpeg_frame_len(buf, p + l1 + l2):
            continue
        pos = p
        while True:
            ln = mpeg_frame_len(buf, pos)
            if not ln:
                break
            if pos + ln > len(buf):
                return _issue('error', 'truncated', 'File is cut off: the last audio frame is incomplete')
            pos += ln
            if pos == len(buf):
                return None
        if len(buf) - pos > 2048:
            return _issue('warn', 'damaged_end', f'{len(buf) - pos} bytes of unknown data after the audio')
        return None
    return None    # couldn't find frames (very short file): no verdict

_OGG_CRC = _table(0x04C11DB7, 32)

def _ogg_crc(data):
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _OGG_CRC[(c >> 24) ^ b]
    return c

def _ogg_end(f, size):
    """The last Ogg page must end exactly at the end of the file, pass its CRC and mark the end of the stream."""
    n = min(size, 70 * 1024)
    f.seek(size - n)
    buf = f.read(n)
    p = len(buf)
    while True:
        p = buf.rfind(b'OggS', 0, p)
        if p < 0 or len(buf) - p < 27:
            return None if p < 0 else _issue('error', 'truncated', 'File is cut off inside the last Ogg page')
        if buf[p + 4] != 0:
            continue
        nseg = buf[p + 26]
        if p + 27 + nseg > len(buf):
            return _issue('error', 'truncated', 'File is cut off inside the last Ogg page')
        end = p + 27 + nseg + sum(buf[p + 27:p + 27 + nseg])
        if end > len(buf):
            return _issue('error', 'truncated', 'File is cut off: the last Ogg page is incomplete')
        page = bytearray(buf[p:end])
        crc = int.from_bytes(page[22:26], 'little')
        page[22:26] = b'\0\0\0\0'
        if _ogg_crc(page) != crc:
            continue          # 'OggS' inside audio data, or a damaged page: look further back
        if end < len(buf):
            return _issue('warn', 'damaged_end', f'{len(buf) - end} bytes of unknown data after the audio')
        if not buf[p + 5] & 4:
            return _issue('warn', 'no_eos', 'The stream has no end marker (file may be cut off)')
        return None

def _mp4_end(f, size):
    """Top-level boxes must add up to the file size and include the index ('moov')."""
    off, seen = 0, set()
    for _ in range(200):
        if off >= size:
            break
        f.seek(off)
        h = f.read(16)
        if len(h) < 8:
            return _issue('error', 'truncated', 'File is cut off inside a box header')
        bsize, btype = int.from_bytes(h[:4], 'big'), h[4:8].decode('latin-1')
        if bsize == 1:
            bsize = int.from_bytes(h[8:16], 'big')
        elif bsize == 0:
            bsize = size - off
        if bsize < 8:
            return _issue('error', 'bad_metadata', f'Damaged box "{btype}"')
        if off + bsize > size:
            return _issue('error', 'truncated', f'File is cut off: the "{btype}" box needs '
                          f'{off + bsize - size:,} more bytes')
        seen.add(btype)
        off += bsize
    if 'moov' not in seen:
        return _issue('error', 'bad_metadata', 'No "moov" box (the file cannot be played)')
    return None

def _chunked_end(f, size, path):
    """WAV / AIFF / DSF record their own length: a shorter file is cut off."""
    f.seek(0)
    h = f.read(20)
    e = formats.ext(path)
    if e == '.wav' and h[:4] in (b'RIFF', b'RF64'):
        claimed = int.from_bytes(h[4:8], 'little') + 8
    elif e in ('.aif', '.aiff', '.aifc') and h[:4] == b'FORM':
        claimed = int.from_bytes(h[4:8], 'big') + 8
    elif e == '.dsf' and h[:4] == b'DSD ':
        claimed = int.from_bytes(h[12:20], 'little')
    else:
        return None
    if h[:4] == b'RF64' or claimed in (8, 0xFFFFFFFF + 8):
        return None                        # size kept elsewhere / unknown (streamed)
    if size < claimed:
        return _issue('error', 'truncated', f'File is cut off: {claimed - size:,} bytes missing')
    return None


def check_other_stream(f, size, path):
    """Check an open MP3 / M4A / Ogg / Opus / WavPack / ... file."""
    from PIL import Image
    issues, info = [], {}
    name = formats.FORMAT_NAME.get(formats.ext(path), 'audio')
    try:
        audio = formats.load(f, path)
    except Exception as e:
        msg = str(e).strip() or type(e).__name__
        return {'issues': [_issue('error', 'not_audio', f'Not a readable {name} file ({msg[:120]})')], 'info': info}
    si = formats.stream_info(audio, path)
    raw_tags = formats.raw_pairs(audio, path)
    tags = {}
    for k, v in raw_tags:
        tags.setdefault(tagmod.canonical(k), v)
    for num, tot in (('TRACKNUMBER', 'TRACKTOTAL'), ('DISCNUMBER', 'DISCTOTAL')):
        a, _, b = tags.get(num, '').partition('/')
        if b.strip().isdigit():
            tags[num] = a.strip()
            tags.setdefault(tot, b.strip())
    pictures = []
    for pic in formats.pictures(audio, path):
        w = h = 0
        try:
            w, h = Image.open(io.BytesIO(pic['data'])).size
        except Exception:
            pass
        pictures.append({'type': int(pic['type']), 'mime': pic['mime'] or '', 'size': len(pic['data']),
                         'real': formats._sniff(pic['data']), 'width': w, 'height': h})
    info.update(sample_rate=si['sample_rate'], bits=si['bits'], channels=si['channels'], seconds=si['seconds'],
                bitrate=si['bitrate'], format=si['format'], pictures=len(pictures),
                tags={k: tags[k] for k in ('TITLE', 'ARTIST', 'ALBUM', 'ALBUMARTIST', 'TRACKNUMBER', 'TRACKTOTAL',
                                           'DISCNUMBER', 'DATE') if k in tags})
    if not si['seconds']:
        issues.append(_issue('error', 'no_audio', 'No audio (length 0:00)'))
    fam, e = formats.family(path), formats.ext(path)
    try:
        if e == '.mp3':
            end = _mp3_end(f, size)
            if getattr(audio.info, 'sketchy', False):
                issues.append(_issue('warn', 'sketchy', 'Could not find clean MP3 frames at the start (damaged or not really MP3)'))
        elif e in ('.ogg', '.oga', '.opus', '.spx'):
            end = _ogg_end(f, size)
        elif fam == 'mp4':
            end = _mp4_end(f, size)
        else:
            end = _chunked_end(f, size, path)
    except Exception:
        end = None
    if end:
        issues.append(end)
    lossless = e in ('.wav', '.aif', '.aiff', '.aifc', '.wv', '.ape', '.dsf') or \
        (fam == 'mp4' and 'alac' in si['codec'])
    _common_checks(issues, tags, raw_tags, pictures, si, lossless=lossless and e != '.dsf')
    if not lossless and si['bitrate'] and si['bitrate'] < (96 if e == '.opus' else 160):
        issues.append(_issue('info', 'low_bitrate', f'Low bitrate: {si["bitrate"]} kbps'))
    return {'issues': issues, 'info': info}


# ── Per file / per album ──────────────────────────────────────────────────────

def check_file(path, size=None):
    try:
        if size is None:
            size = storage.stat(path).size
        with storage.open_file(path, 'rb') as f:
            if formats.family(path) == 'flac':
                return check_flac_stream(f, size)
            return check_other_stream(f, size, path)
    except Exception as e:
        return {'issues': [_issue('error', 'unreadable', f'Could not read the file: {storage.error_message(e, path)}')],
                'info': {}}


check_flac = check_file   # old name

def deep_check_available():
    return shutil.which('flac') is not None or shutil.which('ffmpeg') is not None

def _ffmpeg_check(path):
    try:
        p = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', path, '-f', 'null', '-'],
                           capture_output=True, text=True, timeout=900)
    except Exception as e:
        return _issue('error', 'decode', f'Full decode failed to run: {e}')
    lines = [l.strip() for l in (p.stderr or '').splitlines() if l.strip()]
    if p.returncode != 0 or lines:
        return _issue('error', 'decode', 'Full decode: ' + (lines[0][:200] if lines else 'failed'))
    return None

def deep_check(path):
    """Decode the whole file with `flac -t` (FLAC) or ffmpeg (anything) — local files only."""
    if storage.kind(path) != 'local' or not deep_check_available():
        return None
    if formats.family(path) != 'flac' or not shutil.which('flac'):
        return _ffmpeg_check(path) if shutil.which('ffmpeg') else None
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
    want = 'PNG' if path.lower().endswith('.png') else 'JPEG'
    if fmt != want:
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
        out.append(_issue('info', 'other_audio', f'{len(extras["other_audio"])} audio file(s) in a format this app '
                          f'does not read ({", ".join(kinds)})'))
    if extras.get('variants'):
        out.append(_issue('info', 'cover_variants', 'Cover image under another type or spelling: '
                          + ', '.join(extras['variants'][:6]) + ' — Quick fixes → Tidy turns it into the right file'))
    # the same track in two formats (FLAC + MP3 copies): players list both
    stems = {}
    for r in file_results:
        name = r.get('name') or ''
        stem, dot, ext = name.rpartition('.')
        if dot:
            stems.setdefault(stem.casefold(), []).append(ext.upper())
    dupes = {s: e for s, e in stems.items() if len(e) > 1}
    if dupes:
        ex = next(iter(dupes.items()))
        out.append(_issue('warn', 'same_track_formats', f'{len(dupes)} track(s) are here in more than one format '
                          f'(e.g. "{ex[0]}": {", ".join(ex[1])}) — players will show them twice'))
    fmts = sorted({i.get('format') or 'FLAC' for i in infos})
    if len(fmts) > 1 and not dupes:
        out.append(_issue('info', 'mixed_codecs', 'Album mixes formats: ' + ', '.join(fmts)))
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
_OTHER_AUDIO = ('.aac', '.ac3', '.dts', '.mka', '.tta', '.tak', '.dff', '.mp2', '.mid', '.midi')  # not read

def _album_tasks(album):
    """
    -> ([(kind, path, Entry)] for every FLAC and every required cover file,
        {'junk': [...], 'other_audio': [...]} names found next to them)
    """
    from .scan import cover_name, variants
    tasks, extras = [], {'junk': [], 'other_audio': [], 'variants': []}
    folders = [album['path']] + [d['path'] for d in album.get('disc_info', []) if d['path'] != album['path']]
    for folder in folders:
        files, _ = list_dir(folder)
        prefix = '' if folder == album['path'] else storage.basename(folder) + '/'
        for name, e in sorted(files.items()):
            low = name.lower()
            if is_track(name):
                tasks.append(('track', storage.join(folder, name), e))
            elif low.startswith('._') or low in _JUNK or low.endswith(_JUNK_EXT):
                extras['junk'].append(prefix + name)
            elif low.endswith(_OTHER_AUDIO):
                extras['other_audio'].append(prefix + name)
        extras['variants'] += [prefix + storage.basename(v) for v in variants(folder, files, cover_name())]
        cov = find_cover(folder, files)
        if cov:
            tasks.append(('cover', cov, files[storage.basename(cov)]))
    return tasks, extras


def _run_task(kind, path, entry, deep):
    cache = _load_cache()
    key = ('deep:' if deep and kind == 'track' else '') + path
    fp = entry.fingerprint if entry is not None else None
    hit = cache.get(key)
    if hit and fp and hit.get('fp') == fp and hit.get('size') == entry.size:
        return hit['result']
    if kind == 'cover':
        result = {'issues': check_cover(path), 'info': {}}
    else:
        result = check_file(path, entry.size if entry is not None else None)
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
    tracks = [f for f in files if f['kind'] == 'track']
    multi = any(d.get('path') != album['path'] for d in album.get('disc_info', []))
    return {'issues': album_issues(tracks, album['name'], extras, multi), 'files': files}


class CheckJob:
    def __init__(self, lib, deep):
        self.id = uuid.uuid4().hex[:12]
        self.lib, self.deep = lib, deep
        self.albums = [(a['name'], al) for a in lib.get('artists', []) for al in a.get('albums', [])]
        self.total, self.done, self.current = len(self.albums), 0, ''
        self.status, self.cancelled = 'running', False
        self.report = None
        self.converted = 0
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
                if config.cfg.get('auto_tidy', True) and any(
                        f['kind'] == 'cover' and any(i['code'] == 'cover_not_jpeg' for i in f['issues'])
                        for f in res['files']):
                    # cover.jpg that is really a PNG: re-save it as a real JPEG, then check again
                    try:
                        from .artwork import tidy_album
                        tidy_album(al)
                        res = check_album(al, self.deep, pool)
                        self.converted += 1
                    except Exception:
                        pass
                files_checked += sum(1 for f in res['files'] if f['kind'] == 'track')
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
                       'counts': counts, 'albums': found, 'converted_covers': self.converted}
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
