"""The fast FLAC / cover check."""
import io, shutil, subprocess, time
import pytest
from mutagen.flac import FLAC, Picture
from art_manager import check, config
from conftest import JPEG_BYTES, PNG_1PX

@pytest.fixture(scope='module')
def noise_flac(tmp_path_factory):
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg needed')
    p = tmp_path_factory.mktemp('n') / 'noise.flac'
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'anoisesrc=d=3:r=44100',
                    '-ac', '2', '-c:a', 'flac', str(p)], check=True)
    return p.read_bytes()

def tagged(path, data, n=1, title=True, art=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    f = FLAC(str(path))
    if title:
        f['TITLE'] = f'Song {n}'
    f['ARTIST'] = 'Some Band'; f['ALBUM'] = 'Some Album'; f['TRACKNUMBER'] = str(n)
    if art:
        pic = Picture(); pic.type = 3; pic.mime = 'image/jpeg'; pic.data = JPEG_BYTES
        f.add_picture(pic)
    f.save()
    return path

def codes(res, level=None):
    return [i['code'] for i in res['issues'] if level is None or i['level'] == level]

def test_healthy_file_has_no_issues(tmp_path, noise_flac):
    p = tagged(tmp_path / 'ok.flac', noise_flac)
    assert check.check_file(str(p))['issues'] == []
    info = check.check_file(str(p))['info']
    assert info['sample_rate'] == 44100 and info['channels'] == 2 and info['tags']['TITLE'] == 'Song 1'

@pytest.mark.parametrize('cut,code', [(0.5, 'truncated'), (0.999, 'damaged_end')])
def test_cut_off_file_is_an_error(tmp_path, noise_flac, cut, code):
    p = tagged(tmp_path / 'ok.flac', noise_flac)
    data = p.read_bytes()
    p.write_bytes(data[:int(len(data) * cut)])
    res = check.check_file(str(p))
    assert code in codes(res, 'error'), res

def test_corrupted_last_byte(tmp_path, noise_flac):
    p = tagged(tmp_path / 'ok.flac', noise_flac)
    d = bytearray(p.read_bytes()); d[-1] ^= 0xFF; p.write_bytes(bytes(d))
    assert 'damaged_end' in codes(check.check_file(str(p)), 'error')

def test_not_flac_and_id3(tmp_path, noise_flac):
    p = tmp_path / 'x.flac'; p.write_bytes(b'not audio at all' * 100)
    assert codes(check.check_file(str(p))) == ['not_flac']
    ok = tagged(tmp_path / 'ok.flac', noise_flac).read_bytes()
    p.write_bytes(b'ID3\x03\x00\x00\x00\x00\x00\x0a' + b'\x00' * 10 + ok)
    assert codes(check.check_file(str(p))) == ['id3']

def test_missing_tags_and_art_are_warnings(tmp_path, noise_flac):
    p = tagged(tmp_path / 'x.flac', noise_flac, title=False, art=False)
    res = check.check_file(str(p))
    assert set(codes(res, 'warn')) == {'missing_tags', 'no_art'}
    assert 'title' in next(i['msg'] for i in res['issues'] if i['code'] == 'missing_tags')

def test_album_level_issues(tmp_path, noise_flac):
    root = tmp_path / 'Music' / 'Band' / 'Album'
    tagged(root / '01.flac', noise_flac, n=1); tagged(root / '02.flac', noise_flac, n=1)   # duplicate number
    (root / 'cover.jpg').write_bytes(b'garbage')
    album = {'name': 'Album', 'path': str(root), 'disc_info': [{'path': str(root)}]}
    res = check.check_album(album)
    assert 'dup_tracks' in codes(res)
    cover = next(f for f in res['files'] if f['kind'] == 'cover')
    assert cover['issues'][0]['code'] == 'bad_cover'

def test_small_and_fake_cover(tmp_path):
    from PIL import Image
    p = tmp_path / 'cover.jpg'
    Image.new('RGB', (300, 300)).save(p, 'PNG')          # a PNG named .jpg, and small
    assert {i['code'] for i in check.check_cover(str(p))} == {'cover_not_jpeg', 'small_cover'}
    Image.new('RGB', (800, 800)).save(p, 'JPEG')
    assert check.check_cover(str(p)) == []

def _wait(client, job_id):
    for _ in range(200):
        d = client.get(f'/api/check/{job_id}').json
        if d['status'] != 'running':
            return d
        time.sleep(0.05)
    raise AssertionError('check did not finish')

def test_library_check_job_and_cache(client, tmp_path, noise_flac, monkeypatch):
    root = tmp_path / 'Music'
    tagged(root / 'Band' / 'Good' / '01.flac', noise_flac)
    (root / 'Band' / 'Good' / 'cover.jpg').write_bytes(JPEG_BYTES)
    bad = tagged(root / 'Band' / 'Bad' / '01.flac', noise_flac)
    bad.write_bytes(bad.read_bytes()[:20000])
    monkeypatch.setattr(check, '_cache', None)
    assert client.get('/api/scan', query_string={'root': str(root)}).status_code == 200
    job = client.post('/api/check', json={}).json
    rep = _wait(client, job['id'])['report']
    assert rep['complete'] and rep['albums_checked'] == 2 and rep['files_checked'] == 2
    by_album = {a['album']: a for a in rep['albums']}
    assert 'truncated' in [i['code'] for f in by_album['Bad']['files'] for i in f['issues']]
    assert rep['counts']['error'] >= 1
    # report is kept for the next start
    assert client.get('/api/check').json['report']['files_checked'] == 2
    # second run: nothing unchanged is read again
    calls = []
    real = check.check_file
    monkeypatch.setattr(check, 'check_file', lambda *a, **k: calls.append(a) or real(*a, **k))
    _wait(client, client.post('/api/check', json={}).json['id'])
    assert calls == []
    # a changed file is checked again
    t = time.time() + 10
    import os; os.utime(root / 'Band' / 'Good' / '01.flac', (t, t))
    _wait(client, client.post('/api/check', json={}).json['id'])
    assert len(calls) == 1

def test_check_album_endpoint(client, make_library):
    root = make_library()
    lib = client.get('/api/scan', query_string={'root': str(root)}).json
    al = next(a for a in lib['artists'] if a['name'] == 'Alpha')['albums'][0]
    r = client.post('/api/check-album', json={'name': al['name'], 'path': al['path'], 'disc_info': al['disc_info']})
    assert r.status_code == 200
    assert {f['kind'] for f in r.json['files']} == {'track', 'cover'}
    r = client.post('/api/check-album', json={'path': str(root.parent), 'disc_info': []})
    assert r.status_code == 403   # confined to the library

@pytest.mark.skipif(not shutil.which('flac'), reason='flac tool not installed')
def test_deep_check(tmp_path, noise_flac):
    p = tagged(tmp_path / 'x.flac', noise_flac)
    assert check.deep_check(str(p)) is None

def test_check_converts_png_named_jpg(client, tmp_path, noise_flac, monkeypatch):
    from PIL import Image
    root = tmp_path / 'Music'
    tagged(root / 'Band' / 'Album' / '01.flac', noise_flac)
    b = io.BytesIO(); Image.new('RGB', (600, 600), 'red').save(b, 'PNG')
    (root / 'Band' / 'Album' / 'cover.jpg').write_bytes(b.getvalue())
    monkeypatch.setattr(check, '_cache', None)
    client.get('/api/scan', query_string={'root': str(root)})
    rep = _wait(client, client.post('/api/check', json={}).json['id'])['report']
    assert rep['converted_covers'] == 1
    assert Image.open(root / 'Band' / 'Album' / 'cover.jpg').format == 'JPEG'
    assert 'cover_not_jpeg' not in [i['code'] for a in rep['albums'] for f in a['files'] for i in f['issues']]
