"""
Repair files whose *end* is damaged or cut off (FLAC, MP3).

The only safe repair without the original: cut the file right after its last
intact audio frame, so it ends a fraction of a second early instead of
glitching or failing to play at the end. For FLAC the header is updated to
match — the total length, the seek table, and the MD5 of the audio (cleared,
since it described the full, undamaged audio). Tags and pictures stay.

Damage further in (with intact audio after it) is bridged instead: each
damaged frame is replaced by a valid frame of silence of the same length, so
the file plays through with a short dropout instead of a glitch or a decoder
error. Nothing else in the audio changes. Every repair first saves the original to <data dir>/repair_backups
(kept until undone or the last 20), and the result is checked again; if it
doesn't come out clean, the original is put back.
"""
import json, os, re, shutil, struct, time, uuid

from . import check, config, formats, storage

KEEP_BACKUPS = 20
WINDOW = 8 * 1024 * 1024        # how far back from the end a damaged frame is looked for


class RepairError(RuntimeError):
    pass


# ── FLAC ──────────────────────────────────────────────────────────────────────

def _flac_layout(data):
    """-> (offset of first audio byte, STREAMINFO dict, [(block type, body offset, length)])"""
    off = 0
    if data[:3] == b'ID3':
        off = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9])
    if data[off:off + 4] != b'fLaC':
        raise RepairError('not a FLAC file')
    off += 4
    blocks, si = [], None
    while True:
        if off + 4 > len(data):
            raise RepairError('the metadata is damaged')
        last, btype, blen = data[off] & 0x80, data[off] & 0x7F, int.from_bytes(data[off + 1:off + 4], 'big')
        blocks.append((btype, off + 4, blen))
        if btype == 0:
            d = data[off + 4:off + 4 + 34]
            x = int.from_bytes(d[10:18], 'big')
            si = {'max_blocksize': struct.unpack('>H', d[2:4])[0], 'max_framesize': int.from_bytes(d[7:10], 'big'),
                  'sample_rate': x >> 44, 'total_samples': x & 0xFFFFFFFFF,
                  'channels': ((x >> 41) & 7) + 1, 'bits': ((x >> 36) & 0x1F) + 1}
        off += 4 + blen
        if last:
            break
    if si is None:
        raise RepairError('no STREAMINFO block')
    return off, si, blocks

def _flac_frames(buf, si):
    """
    Walk the frames of buf (the tail of the audio). -> (start of the first bad
    frame in buf, end sample of the last good frame) or (None, None) if
    every frame is fine. Raises if the damage is not at the end.
    """
    cand = []
    pos = 0
    while True:
        a, b = buf.find(b'\xff\xf8', pos), buf.find(b'\xff\xf9', pos)
        q = min(x for x in (a, b, len(buf)) if x >= 0)
        if q >= len(buf):
            break
        h = check.parse_frame_header(buf, q, si)
        if h:
            cand.append((q, h[0]))
        pos = q + 1
    starts = {c[0] for c in cand}
    end_sample = dict(cand)
    t = check._CRC16

    def frame_end(p):
        c, n = 0, len(buf)
        for j in range(p, n - 2):
            c = ((c << 8) & 0xFFFF) ^ t[(c >> 8) ^ buf[j]]
            k = j + 3
            if (k in starts or k == n) and c == (buf[j + 1] << 8 | buf[j + 2]):
                return k
        return None

    # a real frame to start from: one whose next frame is valid too
    p = None
    for q, _ in cand[:64]:
        e = frame_end(q)
        if e is not None and e != len(buf) and frame_end(e) is not None:
            p = q
            break
    if p is None:
        raise RepairError('no intact audio found near the end — the damage is too large to repair')
    last_good = None
    while True:
        e = frame_end(p)
        if e is None:
            break
        last_good = p
        if e == len(buf):
            return None, None
        p = e
    # p is the first bad frame. Anything intact after it? Then the damage is in the middle.
    for q, _ in cand:
        if q > p:
            e = frame_end(q)
            if e is not None and (e == len(buf) or frame_end(e) is not None):
                raise RepairError('the damage is not at the very end (intact audio follows it) — '
                                  'cutting would lose music; replace this file instead')
    return p, end_sample[last_good]

def repair_flac_bytes(data):
    """-> (new bytes, seconds removed). Raises RepairError if it can't or needn't be repaired."""
    audio_off, si, blocks = _flac_layout(data)
    end = len(data)
    if end - 128 >= audio_off and data[end - 128:end - 125] == b'TAG':
        end -= 128
    if end - 32 >= audio_off and data[end - 32:end - 24] == b'APETAGEX':
        end -= int.from_bytes(data[end - 20:end - 16], 'little') + 32
    start = max(audio_off, end - WINDOW)
    buf = data[start:end].rstrip(b'\0')
    bad, new_total = _flac_frames(buf, si)
    if bad is None:
        raise RepairError('the end of this file is intact — nothing to repair')
    cut = start + bad
    old_total = si['total_samples']
    out = bytearray(data[:cut])
    for btype, body, blen in blocks:
        if btype == 0:                       # STREAMINFO: total samples, MD5 unknown
            x = int.from_bytes(out[body + 10:body + 18], 'big')
            x = (x & ~0xFFFFFFFFF) | (new_total & 0xFFFFFFFFF)
            out[body + 10:body + 18] = x.to_bytes(8, 'big')
            out[body + 18:body + 34] = b'\0' * 16
        elif btype == 3:                     # SEEKTABLE: points past the new end become placeholders
            for i in range(blen // 18):
                o = body + i * 18
                sample = int.from_bytes(out[o:o + 8], 'big')
                if sample != 0xFFFFFFFFFFFFFFFF and (sample >= new_total or
                                                     audio_off + int.from_bytes(out[o + 8:o + 16], 'big') >= cut):
                    out[o:o + 8] = b'\xff' * 8
                    out[o + 8:o + 18] = b'\0' * 10
    removed = (old_total - new_total) / si['sample_rate'] if old_total and si['sample_rate'] else 0.0
    return bytes(out), max(0.0, removed)


# ── FLAC: damaged frames in the middle -> silence ─────────────────────────────

def _utf8_num(n):
    """FLAC's UTF-8-like coding of a frame / sample number."""
    if n < 0x80:
        return bytes([n])
    for nbytes, lead in ((2, 0xC0), (3, 0xE0), (4, 0xF0), (5, 0xF8), (6, 0xFC), (7, 0xFE)):
        if n < (1 << (5 * nbytes + 1)) or nbytes == 7:
            out = []
            for _ in range(nbytes - 1):
                out.append(0x80 | (n & 0x3F)); n >>= 6
            return bytes([lead | n] + out[::-1])

def _header_fields(buf, pos):
    """Template fields of a valid frame header: (variable, bs_code, sr_code, b3, bs, extra sample-rate bytes)."""
    b1, b2, b3 = buf[pos + 1], buf[pos + 2], buf[pos + 3]
    i = pos + 4
    first = buf[i]
    ones = 0
    while ones < 8 and first & (0x80 >> ones):
        ones += 1
    i += 1 + (ones - 1 if ones > 1 else 0)
    bs_code, sr_code = b2 >> 4, b2 & 0x0F
    if bs_code == 1:
        bs = 192
    elif 2 <= bs_code <= 5:
        bs = 576 << (bs_code - 2)
    elif bs_code == 6:
        bs = buf[i] + 1; i += 1
    elif bs_code == 7:
        bs = (buf[i] << 8 | buf[i + 1]) + 1; i += 2
    else:
        bs = 256 << (bs_code - 8)
    sr_len = 1 if sr_code == 12 else 2 if sr_code in (13, 14) else 0
    return {'variable': b1 & 1, 'bs_code': bs_code, 'sr_code': sr_code, 'b3': b3, 'bs': bs,
            'sr_extra': bytes(buf[i:i + sr_len])}

_SS_BITS = {1: 8, 2: 12, 4: 16, 5: 20, 6: 24, 7: 32}

def silent_frame(t, number, bs, si):
    """A valid FLAC frame of `bs` samples of silence, shaped like the template header t."""
    if bs == t['bs'] and t['bs_code'] not in (6, 7):
        bs_code, bs_extra = t['bs_code'], b''
    else:
        bs_code, bs_extra = 7, (bs - 1).to_bytes(2, 'big')
    head = bytes([0xFF, 0xF8 | t['variable'], (bs_code << 4) | t['sr_code'], t['b3']]) + _utf8_num(number) \
        + bs_extra + t['sr_extra']
    head += bytes([check.crc8(head)])
    ch_assign, ss = t['b3'] >> 4, (t['b3'] >> 1) & 7
    bps = _SS_BITS.get(ss, si['bits'])
    n_ch = ch_assign + 1 if ch_assign < 8 else 2
    side = {8: 1, 9: 0, 10: 1}.get(ch_assign)       # the side channel needs one more bit
    bits = ''
    for ch in range(n_ch):
        width = bps + (1 if ch == side else 0)
        bits += '0' + '000000' + '0' + '0' * width  # zero pad, CONSTANT subframe, no wasted bits, value 0
    bits += '0' * (-len(bits) % 8)
    body = int(bits, 2).to_bytes(len(bits) // 8, 'big')
    frame = head + body
    return frame + check.crc16(frame).to_bytes(2, 'big')

def conceal_flac_bytes(data):
    """
    Replace damaged frames anywhere in the audio with silent frames of the
    same length (a short dropout instead of a glitch / decoder error).
    -> (new bytes, [(start sample, samples)] replaced). Raises if there is
    nothing to do or the damage can't be bridged (then the end-cut is used).
    """
    audio_off, si, blocks = _flac_layout(data)
    end = len(data)
    if end - 128 >= audio_off and data[end - 128:end - 125] == b'TAG':
        end -= 128
    if end - 32 >= audio_off and data[end - 32:end - 24] == b'APETAGEX':
        end -= int.from_bytes(data[end - 20:end - 16], 'little') + 32
    buf = data[audio_off:end].rstrip(b'\0')
    cand = []
    pos = 0
    while True:
        a, b = buf.find(b'\xff\xf8', pos), buf.find(b'\xff\xf9', pos)
        q = min(x for x in (a, b, len(buf)) if x >= 0)
        if q >= len(buf):
            break
        h = check.parse_frame_header(buf, q, si)
        if h:
            cand.append(q)
        pos = q + 1
    starts = set(cand)
    t16 = check._CRC16

    def frame_end(p):
        c, n = 0, len(buf)
        for j in range(p, n - 2):
            c = ((c << 8) & 0xFFFF) ^ t16[(c >> 8) ^ buf[j]]
            k = j + 3
            if (k in starts or k == n) and c == (buf[j + 1] << 8 | buf[j + 2]):
                return k
        return None

    def first_sample(p):
        f = _header_fields(buf, p)
        e = check.parse_frame_header(buf, p, si)[0]
        return e - f['bs'], f

    if 0 not in starts or frame_end(0) is None:
        raise RepairError('the first audio frame is damaged — replace this file')
    regions, p, prev_end_sample, template = [], 0, 0, None
    while True:
        e = frame_end(p)
        if e is not None:
            prev_end_sample = check.parse_frame_header(buf, p, si)[0]
            f = _header_fields(buf, p)
            if template is None or (not f['variable'] and f['bs'] == si['max_blocksize']):
                template = f                   # a full-size frame: the shape of the silent ones
            if e == len(buf):
                break
            p = e
            continue
        # p starts a damaged stretch: the next real frame is a header whose frame and the one after check out
        q = next((c for c in cand if c > p and (lambda fe: fe is not None and (fe == len(buf) or frame_end(fe) is not None))(frame_end(c))), None)
        if q is None:
            raise RepairError('no intact audio after the damage')
        q_start, _ = first_sample(q)
        regions.append((p, q, prev_end_sample, q_start))
        p = q
    if not regions:
        raise RepairError('the audio is intact — nothing to repair')
    nominal = None if template['variable'] else si['max_blocksize']
    out, last, deltas = bytearray(), 0, []      # deltas: (old end of region, size change)
    for p, q, s0, s1 in regions:
        if s1 - s0 <= 0 or s1 - s0 > 30 * si['sample_rate']:
            raise RepairError('the damaged stretch is too long to bridge with silence — replace this file')
        if nominal and (s0 % nominal or s1 % nominal):
            raise RepairError('unexpected frame layout around the damage — replace this file')
        silence = silent_bytes_for(template, s0, s1, nominal, si)
        out += buf[last:p] + silence
        deltas.append((q, len(silence) - (q - p)))
        last = q
    out += buf[last:]
    # header: the MD5 no longer applies; seek points after a changed stretch move by its size change
    head = bytearray(data[:audio_off])
    for btype, body, blen in blocks:
        if btype == 0:
            head[body + 18:body + 34] = b'\0' * 16
        elif btype == 3:
            for i in range(blen // 18):
                o = body + i * 18
                if head[o:o + 8] == b'\xff' * 8:
                    continue
                off = int.from_bytes(head[o + 8:o + 16], 'big')
                head[o + 8:o + 16] = (off + sum(d for q, d in deltas if off >= q)).to_bytes(8, 'big')
    return bytes(head) + bytes(out), [(s0, s1 - s0) for _, _, s0, s1 in regions]

def silent_bytes_for(template, s0, s1, nominal, si):
    out, n = b'', s0
    while n < s1:
        bs = min(nominal or 4096, s1 - n)
        out += silent_frame(template, n // nominal if nominal else n, bs, si)
        n += bs
    return out


# ── MP3 ───────────────────────────────────────────────────────────────────────

def repair_mp3_bytes(data):
    """Cut an MP3 after its last complete frame (and drop what follows it)."""
    end = len(data)
    tail_tags = b''
    if end >= 128 and data[end - 128:end - 125] == b'TAG':
        tail_tags = data[end - 128:end]      # keep the ID3v1 tag
        end -= 128
    start = max(0, end - WINDOW)
    buf = data[start:end]
    p = None
    for i in range(0, max(0, len(buf) - 4)):
        l1 = check.mpeg_frame_len(buf, i)
        if l1 and check.mpeg_frame_len(buf, i + l1) and check.mpeg_frame_len(buf, i + l1 + check.mpeg_frame_len(buf, i + l1)):
            p = i
            break
    if p is None:
        raise RepairError('no intact MP3 frames found near the end')
    while True:
        ln = check.mpeg_frame_len(buf, p)
        if not ln or p + ln > len(buf):
            break
        p += ln
        if p == len(buf):
            raise RepairError('the end of this file is intact — nothing to repair')
    cut = start + p
    return data[:cut] + tail_tags, 0.0    # at most one frame (~26 ms) is lost


# ── Backups, apply, undo ──────────────────────────────────────────────────────

def _backup_dir():
    d = os.path.join(config.DATA_DIR, 'repair_backups')
    os.makedirs(d, exist_ok=True)
    return d

def _prune():
    entries = sorted(e for e in os.listdir(_backup_dir()) if e.endswith('.json'))
    for name in entries[:-KEEP_BACKUPS]:
        bid = name[:-5]
        for f in (name, bid + '.bin'):
            try: os.remove(os.path.join(_backup_dir(), f))
            except OSError: pass

def can_repair(path):
    return formats.ext(path) in ('.flac', '.mp3')

def repair(path):
    """
    Repair one file. -> {'path', 'status': 'repaired'|'skipped'|'error', 'message', 'backup', 'seconds'}
    """
    out = {'path': path, 'backup': None, 'seconds': 0.0}
    if not can_repair(path):
        return dict(out, status='skipped', message='only FLAC and MP3 files can be repaired — replace this one instead')
    try:
        data = storage.read_bytes(path)
        concealed = None
        if formats.ext(path) == '.flac':
            try:
                new, seconds = repair_flac_bytes(data)
            except RepairError as e:
                if 'not at the very end' not in str(e):
                    raise
                new, concealed = conceal_flac_bytes(data)
                seconds = 0.0
        else:
            new, seconds = repair_mp3_bytes(data)
    except RepairError as e:
        return dict(out, status='skipped', message=str(e))
    except Exception as e:
        return dict(out, status='error', message=storage.error_message(e, path))

    bid = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
    with open(os.path.join(_backup_dir(), bid + '.bin'), 'wb') as f:
        f.write(data)
    with open(os.path.join(_backup_dir(), bid + '.json'), 'w', encoding='utf-8') as f:
        json.dump({'id': bid, 'path': path, 'size': len(data), 'created': time.time()}, f)
    try:
        storage.write_bytes(path, new)
        res = check.check_file(path)
        bad = [i for i in res['issues'] if i['level'] == 'error']
        if bad:
            storage.write_bytes(path, data)
            return dict(out, status='error', message='the repaired file still had errors — original put back: ' + bad[0]['msg'])
    except Exception as e:
        try:
            storage.write_bytes(path, data)
        except Exception:
            pass
        return dict(out, status='error', message=storage.error_message(e, path))
    _prune()
    if concealed:
        rate = _flac_layout(new)[1]['sample_rate'] or 1
        where = ', '.join(f'{int(s0 / rate) // 60}:{int(s0 / rate) % 60:02d} ({n / rate:.2f} s)' for s0, n in concealed)
        return dict(out, status='repaired', backup=bid, seconds=round(sum(n for _, n in concealed) / rate, 2),
                    message=f'replaced the damaged audio with a moment of silence at {where}')
    return dict(out, status='repaired', backup=bid, seconds=round(seconds, 2),
                message=f'cut {len(data) - len(new):,} damaged bytes'
                        + (f' ({seconds:.2f} s of audio)' if seconds else ''))

def undo(backup_id):
    if not re.match(r'^[\w.-]+$', backup_id or ''):
        raise ValueError('bad backup id')
    meta_file = os.path.join(_backup_dir(), backup_id + '.json')
    with open(meta_file, encoding='utf-8') as f:
        meta = json.load(f)
    with open(os.path.join(_backup_dir(), backup_id + '.bin'), 'rb') as f:
        storage.write_bytes(meta['path'], f.read())
    for p in (meta_file, os.path.join(_backup_dir(), backup_id + '.bin')):
        try: os.remove(p)
        except OSError: pass
    return meta
