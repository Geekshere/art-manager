"""Repairing FLAC / MP3 files whose end is damaged or cut off."""
import shutil, subprocess, time

import pytest

from art_manager import check, repair

@pytest.fixture(scope='module')
def noise(tmp_path_factory):
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg needed')
    d = tmp_path_factory.mktemp('rep')
    out = {}
    for ext, codec in (('flac', ['-c:a', 'flac']), ('mp3', ['-c:a', 'libmp3lame', '-b:a', '192k'])):
        p = d / f'n.{ext}'
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'anoisesrc=d=4:r=44100',
                        '-ac', '2', *codec, str(p)], check=True)
        out[ext] = p.read_bytes()
    return out

def errors(p):
    return [i['code'] for i in check.check_file(str(p))['issues'] if i['level'] == 'error']

def flipped(data, at):
    b = bytearray(data); b[at] ^= 0xFF
    return bytes(b)

@pytest.mark.parametrize('how', ['cut', 'flip'])
def test_flac_end_repaired(tmp_path, noise, how):
    data = noise['flac']
    p = tmp_path / 'a.flac'
    p.write_bytes(data[:-5000] if how == 'cut' else flipped(data, len(data) - 100))
    assert errors(p)
    r = repair.repair(str(p))
    assert r['status'] == 'repaired', r
    assert errors(p) == []
    if shutil.which('flac'):
        assert subprocess.run(['flac', '-t', '-s', str(p)], capture_output=True).returncode == 0
    repair.undo(r['backup'])                       # undo gives back the exact original
    assert errors(p)

def test_damage_in_the_middle_is_left_alone(tmp_path, noise):
    data = noise['flac']
    p = tmp_path / 'm.flac'
    bad = flipped(data, len(data) // 2)
    p.write_bytes(bad)
    r = repair.repair(str(p))
    assert r['status'] == 'skipped' and 'not at the very end' in r['message']
    assert p.read_bytes() == bad

def test_intact_file_is_not_touched(tmp_path, noise):
    p = tmp_path / 'ok.flac'
    p.write_bytes(noise['flac'])
    r = repair.repair(str(p))
    assert r['status'] == 'skipped' and p.read_bytes() == noise['flac']

def test_mp3_cut_off(tmp_path, noise):
    p = tmp_path / 'a.mp3'
    p.write_bytes(noise['mp3'][:-777])
    assert 'truncated' in errors(p)
    assert repair.repair(str(p))['status'] == 'repaired' and errors(p) == []

def test_other_formats_skipped(tmp_path):
    p = tmp_path / 'a.ogg'
    p.write_bytes(b'x')
    assert repair.repair(str(p))['status'] == 'skipped'

def test_repair_api(client, make_library, noise):
    root = make_library()
    client.get('/api/scan', query_string={'root': str(root)})
    f = next(root.rglob('*.flac'))
    f.write_bytes(noise['flac'][:-3000])
    job = client.post('/api/repair', json={'paths': [str(f)]}).json
    for _ in range(200):
        d = client.get(f"/api/repair/{job['id']}").json
        if d['status'] != 'running':
            break
        time.sleep(0.05)
    assert d['results'][0]['status'] == 'repaired'
    assert client.post('/api/repair/undo', json={'backup': d['results'][0]['backup']}).status_code == 200
    assert f.read_bytes() == noise['flac'][:-3000]
    assert client.post('/api/repair', json={'paths': [str(root.parent / 'x.flac')]}).status_code in (400, 403)
