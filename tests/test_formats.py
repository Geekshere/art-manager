"""MP3, M4A, Ogg, Opus, WavPack, WAV, AIFF, WMA: tags, art, checks, scanning."""
import shutil, subprocess

import pytest

from art_manager import artwork, check, formats, scan, tags
from conftest import JPEG_BYTES

CODEC = {'mp3': ['-c:a', 'libmp3lame', '-b:a', '192k'], 'm4a': ['-c:a', 'aac', '-b:a', '192k'],
         'ogg': ['-c:a', 'libvorbis', '-q:a', '6'], 'opus': ['-c:a', 'libopus', '-b:a', '128k'],
         'wv': ['-c:a', 'wavpack'], 'wav': [], 'aiff': [], 'wma': ['-c:a', 'wmav2', '-b:a', '192k']}

@pytest.fixture(scope='module')
def samples(tmp_path_factory):
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg needed')
    d = tmp_path_factory.mktemp('fmt')
    out = {}
    for ext, codec in CODEC.items():
        p = d / f'src.{ext}'
        r = subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'anoisesrc=d=4:r=44100',
                            '-ac', '2', *codec, str(p)])
        if r.returncode == 0:
            out[ext] = p.read_bytes()
    return out

def fresh(tmp_path, samples, ext, name='01 Song'):
    if ext not in samples:
        pytest.skip(f'ffmpeg cannot write {ext}')
    p = tmp_path / f'{name}.{ext}'
    p.write_bytes(samples[ext])
    return str(p)

ALL = list(CODEC)
FULL = {'TITLE': ['Song'], 'ARTIST': ['Band'], 'ALBUM': ['Album'], 'ALBUMARTIST': ['Band'],
        'TRACKNUMBER': ['3'], 'TRACKTOTAL': ['12'], 'DISCNUMBER': ['1'], 'DATE': ['2013'],
        'MUSICBRAINZ_ALBUMID': ['0b1c'], 'MUSICBRAINZ_ALBUMCOMMENT': ['2013 Remaster'], 'LABEL': ['Label'],
        'CATALOGNUMBER': ['CAT 1']}


@pytest.mark.parametrize('ext', ALL)
def test_write_read_round_trip(tmp_path, samples, ext):
    p = fresh(tmp_path, samples, ext)
    tags.write(p, FULL)
    got, notes = tags.normalize(tags.read_raw(p))
    want = dict(FULL)
    if ext == 'wma':                     # WMA has no total fields
        want.pop('TRACKTOTAL')
    assert got == want
    assert not [n for n in notes if 'renamed' in n]       # native names aren't "renames"

@pytest.mark.parametrize('ext', [e for e in ALL if e != 'wma'])
def test_embed_keeps_tags_and_tag_write_keeps_art(tmp_path, samples, ext):
    p = fresh(tmp_path, samples, ext)
    tags.write(p, FULL)
    artwork.embed_into(p, JPEG_BYTES, 'image/jpeg')
    assert tags.normalize(tags.read_raw(p))[0]['ALBUM'] == ['Album']
    tags.write(p, {**FULL, 'ALBUM': ['New']})
    pics = artwork.track_pictures(p)
    assert len(pics) == 1 and pics[0]['data'] == JPEG_BYTES and pics[0]['type'] == 3

@pytest.mark.parametrize('ext', ALL)
def test_healthy_file_checks_clean(tmp_path, samples, ext):
    p = fresh(tmp_path, samples, ext)
    tags.write(p, FULL)
    if formats.can_embed(p):
        artwork.embed_into(p, JPEG_BYTES, 'image/jpeg')
    res = check.check_file(p)
    bad = [i for i in res['issues'] if i['level'] != 'info' and i['code'] != 'no_art']
    assert bad == []
    assert res['info']['tags']['TRACKNUMBER'] == '3'
    if ext != 'wma':
        assert res['info']['tags']['TRACKTOTAL'] == '12'
    assert res['info']['seconds'] > 3

@pytest.mark.parametrize('ext', ['mp3', 'm4a', 'ogg', 'opus', 'wav', 'aiff'])
def test_cut_off_file_is_found(tmp_path, samples, ext):
    p = fresh(tmp_path, samples, ext)
    tags.write(p, FULL)
    data = open(p, 'rb').read()
    open(p, 'wb').write(data[:int(len(data) * 0.7)])
    codes = [i['code'] for i in check.check_file(p)['issues'] if i['level'] in ('error', 'warn')]
    assert {'truncated', 'damaged_end', 'no_eos', 'not_audio'} & set(codes), codes

def test_garbage_with_audio_extension(tmp_path):
    p = tmp_path / 'x.mp3'
    p.write_bytes(b'not audio at all' * 100)
    codes = [i['code'] for i in check.check_file(str(p))['issues']]
    assert codes and set(codes) & {'not_audio', 'no_audio', 'sketchy'}

def test_mp3_duplicate_album_artist_is_found_and_merged(tmp_path, samples):
    from mutagen.id3 import ID3, TPE2, TXXX
    p = fresh(tmp_path, samples, 'mp3')
    tags.write(p, FULL)
    t = ID3(p); t.add(TXXX(encoding=3, desc='ALBUM ARTIST', text=['Band'])); t.add(TXXX(encoding=3, desc='iTunNORM', text=['x'])); t.save()
    codes = [c for _, c, _ in tags.tag_problems(tags.read_raw(p))]
    assert 'tag_spellings' in codes
    clean, notes = tags.normalize(tags.read_raw(p))
    tags.write(p, clean)
    raw = tags.read_raw(p)
    assert [k for k, _ in raw].count('TPE2') == 1 and not any(k == 'TXXX:ALBUM ARTIST' for k, _ in raw)
    assert ('TXXX:iTunNORM', 'x') in raw            # unknown frames survive untouched

def test_backup_and_undo_for_mp3(tmp_path, samples, monkeypatch):
    from art_manager import config
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path / 'data'))
    p = fresh(tmp_path, samples, 'mp3')
    tags.write(p, FULL)
    before = tags.read_raw(p)
    bid, errors = tags.apply([{'path': p, 'tags': {**FULL, 'TITLE': ['Changed']}}])
    assert not errors and tags.normalize(tags.read_raw(p))[0]['TITLE'] == ['Changed']
    tags.undo(bid)
    assert tags.read_raw(p) == before

def test_mixed_formats_in_one_album(tmp_path, samples, monkeypatch):
    from art_manager.config import cfg
    monkeypatch.setitem(cfg, 'ignore_patterns', [])
    alb = tmp_path / 'Artist' / 'Album'
    alb.mkdir(parents=True)
    for ext in ('mp3', 'm4a'):
        p = fresh(alb, samples, ext)
        tags.write(p, FULL)
    files, _ = scan.list_dir(str(alb))
    assert sum(scan.is_track(f) for f in files) == 2
    res = check.check_album({'path': str(alb), 'name': 'Album', 'disc_info': [{'path': str(alb)}]})
    assert 'same_track_formats' in [i['code'] for i in res['issues']]

def test_canonical_native_names():
    assert tags.canonical('TPE2') == 'ALBUMARTIST'
    assert tags.canonical('aART') == 'ALBUMARTIST'
    assert tags.canonical('WM/AlbumArtist') == 'ALBUMARTIST'
    assert tags.canonical('TXXX:MusicBrainz Album Type') == 'RELEASETYPE'
    assert tags.canonical('----:com.apple.iTunes:CATALOGNUMBER') == 'CATALOGNUMBER'
    assert tags.canonical('TXXX:iTunNORM') == 'TXXX:iTunNORM'
    assert tags.canonical('album_artist') == 'ALBUMARTIST'

def test_mpeg_frame_length():
    # MPEG-1 layer III, 128 kbps, 44.1 kHz, no padding -> 417 bytes
    assert check.mpeg_frame_len(bytes([0xFF, 0xFB, 0x90, 0x00]), 0) == 417
    assert check.mpeg_frame_len(bytes([0xFF, 0xFB, 0xF0, 0x00]), 0) is None   # bad bitrate index
