"""Library types: which file names count as covers, where they are written, embedding on/off."""
import json
from mutagen.flac import FLAC
from art_manager import config
from conftest import JPEG_BYTES, PNG_1PX, add_flac

def scan(client, root, full=False):
    r = client.get('/api/scan', query_string={'root': str(root), **({'full': '1'} if full else {})})
    assert r.status_code == 200, r.json
    return r.json

def album(lib, a, al):
    ar = next(x for x in lib['artists'] if x['name'] == a)
    return next(x for x in ar['albums'] if x['name'] == al)

def artist(lib, a):
    return next(x for x in lib['artists'] if x['name'] == a)

def test_presets(client):
    r = client.post('/api/config', json={'library_type': 'jellyfin'}).json
    assert (r['library_type'], r['cover_name'], r['artist_image_name'], r['disc_covers']) == \
        ('jellyfin', 'folder.jpg', 'folder.jpg', False)
    r = client.post('/api/config', json={'library_type': 'plex'}).json
    assert (r['cover_name'], r['artist_image_name'], r['disc_covers']) == ('cover.jpg', 'artist.jpg', False)
    assert set(r['library_presets']) == {'navidrome', 'plex', 'jellyfin', 'custom'}

def test_custom_names_validated(client):
    r = client.post('/api/config', json={'library_type': 'custom', 'naming': {'cover_name': 'front.png', 'artist_image_name': 'band.jpg', 'disc_covers': False, 'embed_art': False}}).json
    assert (r['cover_name'], r['artist_image_name'], r['disc_covers'], r['embed_art']) == ('front.png', 'band.jpg', False, False)
    bad = client.post('/api/config', json={'library_type': 'custom', 'naming': {'cover_name': '../evil.jpg'}})
    assert bad.status_code == 400 and config.cfg['cover_name'] == 'front.png'

def test_jellyfin_names_decide_what_counts(client, make_library):
    root = make_library()
    client.post('/api/config', json={'library_type': 'jellyfin'})
    lib = scan(client, root)
    assert album(lib, 'Alpha', 'First')['has_cover'] is False            # has cover.jpg, needs folder.jpg
    assert album(lib, 'Alpha', 'First')['cover_path'].endswith('cover.jpg')   # still shown
    assert artist(lib, 'Alpha')['has_img'] is False                       # artist.jpg, needs folder.jpg
    (root / 'Alpha' / 'First' / 'folder.jpg').write_bytes(JPEG_BYTES)
    (root / 'Alpha' / 'folder.jpg').write_bytes(JPEG_BYTES)
    lib = scan(client, root, full=True)
    assert album(lib, 'Alpha', 'First')['has_cover'] and artist(lib, 'Alpha')['has_img']

def test_disc_covers_off_means_album_folder_is_enough(client, make_library):
    root = make_library()
    m = root / '100% Orange Juice' / 'Multi'
    (m / 'cover.jpg').write_bytes(JPEG_BYTES)                         # CD 2 still has none
    assert album(scan(client, root), '100% Orange Juice', 'Multi')['has_cover'] is False   # navidrome: discs too
    client.post('/api/config', json={'library_type': 'plex'})
    assert album(scan(client, root), '100% Orange Juice', 'Multi')['has_cover'] is True    # plex: album folder only

def test_writing_follows_names_and_embed_setting(client, make_library):
    import base64
    root = make_library()
    client.post('/api/config', json={'library_type': 'custom', 'naming': {
        'cover_name': 'folder.png', 'artist_image_name': 'band.jpg', 'disc_covers': False, 'embed_art': False}})
    lib = scan(client, root)
    m = album(lib, '100% Orange Juice', 'Multi')
    before = [p.data for p in FLAC(str(root / '100% Orange Juice' / 'Multi' / 'CD 2' / '01.flac')).pictures]
    r = client.post('/api/prepare-embed', json={'album_path': m['path'], 'disc_info': m['disc_info'], 'source': 'upload',
                                                'image_data': 'data:image/png;base64,' + base64.b64encode(PNG_1PX).decode()}).json
    assert r['total_tracks'] == 0 and r['cover_path'].endswith('folder.png')
    out = root / '100% Orange Juice' / 'Multi'
    assert (out / 'folder.png').read_bytes()[:8] == b'\x89PNG\r\n\x1a\n'          # really a PNG
    assert not (out / 'CD 1' / 'folder.png').exists() and not (out / 'cover.jpg').exists()
    assert [p.data for p in FLAC(str(out / 'CD 2' / '01.flac')).pictures] == before   # not embedded
    r = client.post('/api/set-artist-img', json={'artist_path': str(root / 'Alpha'),
                                                'image_data': 'data:image/png;base64,' + base64.b64encode(PNG_1PX).decode()}).json
    assert r['path'].endswith('band.jpg') and (root / 'Alpha' / 'band.jpg').read_bytes()[:3] == b'\xff\xd8\xff'

def test_changing_names_invalidates_cached_scan(client, make_library):
    root = make_library()
    scan(client, root)
    assert client.get('/api/library').json['artists']
    client.post('/api/config', json={'library_type': 'jellyfin'})
    assert client.get('/api/library').json == {}          # page rescans with the new names

def test_macos_junk_files_are_not_tracks(client, make_library):
    root = make_library()
    (root / 'Alpha' / 'First' / '._01 - One.flac').write_bytes(b'\x00\x05\x16\x07 AppleDouble')
    assert album(scan(client, root), 'Alpha', 'First')['total_tracks'] == 2

def test_existing_setup_migrates_to_navidrome(client):
    with open(config.CFG_FILE, 'w') as f:
        json.dump({'library_root': '/music', 'settings_version': 2}, f)
    config.load_cfg()
    assert config.cfg['library_type'] == 'navidrome' and config.cfg['cover_name'] == 'cover.jpg'
