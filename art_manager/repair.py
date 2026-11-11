"""
Repair files whose *end* is damaged or cut off (FLAC, MP3).

The only safe repair without the original: cut the file right after its last
intact audio frame, so it ends a fraction of a second early instead of
glitching or failing to play at the end. For FLAC the header is updated to
match — the total length, the seek table, and the MD5 of the audio (cleared,
since it described the full, undamaged audio). Tags and pictures stay.

Damage in the middle of a file is not touched: cutting there would lose real
music. Every repair first saves the original to <data dir>/repair_backups
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
                  'sample_rate': x >> 44, 'total_samples': x & 0xFFFFFFFFF}
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
        if formats.ext(path) == '.flac':
            new, seconds = repair_flac_bytes(data)
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
