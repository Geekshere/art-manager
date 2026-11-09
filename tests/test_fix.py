"""Bulk cover fix: fills in missing cover.jpg files, never overwrites one."""
import time
from mutagen.flac import FLAC
from art_manager.artwork import fix_album_covers, to_jpeg
from conftest import JPEG_BYTES, PNG_1PX, add_flac

def _album(root):
    m = root / '100% Orange Juice' / 'Multi'
    return {'path': str(m), 'disc_info': [{'path': str(m / 'CD 1')}, {'path': str(m / 'CD 2')}]}

def test_multidisc_gets_cover_everywhere_without_overwriting(make_library):
    root = make_library()
    m = root / '100% Orange Juice' / 'Multi'
    own = b'\xff\xd8\xff\xe0' + b'\x01' * 64 + b'\xff\xd9'
    (m / 'CD 1' / 'cover.jpg').write_bytes(own)                  # CD 1 has its own art
    r = fix_album_covers(_album(root))
    assert r['status'] == 'fixed' and r['embedded'] == 2
    assert (m / 'CD 1' / 'cover.jpg').read_bytes() == own        # untouched
    assert (m / 'cover.jpg').read_bytes() == own                 # album folder took CD 1's art
    assert (m / 'CD 2' / 'cover.jpg').read_bytes() == own
    for d in ('CD 1', 'CD 2'):
        assert [p.data for p in FLAC(str(m / d / '01.flac')).pictures] == [own]

def test_folder_jpg_and_png_are_used(make_library):
    root = make_library()
    d = root / 'Alpha' / 'Second'
    (d / 'folder.png').write_bytes(PNG_1PX)
    r = fix_album_covers({'path': str(d), 'disc_info': [{'path': str(d)}]}, embed=False)
    assert r['status'] == 'fixed' and r['embedded'] == 0
    assert (d / 'cover.jpg').read_bytes() == to_jpeg(PNG_1PX, 'image/png')

def test_embedded_art_used_when_no_file(make_library):
    root = make_library()
    d = root / 'Alpha' / 'Second'                                # only has embedded PNG art
    r = fix_album_covers({'path': str(d), 'disc_info': [{'path': str(d)}]}, embed=False)
    assert r['status'] == 'fixed' and (d / 'cover.jpg').exists()
    r2 = fix_album_covers({'path': str(d), 'disc_info': [{'path': str(d)}]}, embed=False, use_embedded=False)
    assert r2['status'] == 'fixed' and r2['written'] == []       # nothing left to do

def test_nothing_to_use_is_skipped(make_library):
    root = make_library()
    d = root / '!!!' / 'Thr!!!er'
    r = fix_album_covers({'path': str(d), 'disc_info': [{'path': str(d)}]})
    assert r['status'] == 'skipped' and not (d / 'cover.jpg').exists()

def test_fix_job_api(client, make_library):
    root = make_library()
    client.get('/api/scan', query_string={'root': str(root)})
    al = _album(root)
    job = client.post('/api/fix-covers', json={'albums': [dict(al, label='Multi')], 'embed': True}).json
    for _ in range(200):
        d = client.get(f"/api/fix-covers/{job['id']}").json
        if d['status'] != 'running':
            break
        time.sleep(0.05)
    assert d['status'] == 'done' and d['counts'] == {'fixed': 1} and d['tracks'] == 2
    lib = client.get('/api/scan', query_string={'root': str(root)}).json
    m = next(a for a in lib['artists'] if a['name'] == '100% Orange Juice')['albums'][0]
    assert m['has_cover'] is True
    bad = client.post('/api/fix-covers', json={'albums': [{'path': str(root.parent)}]})
    assert bad.status_code == 403
