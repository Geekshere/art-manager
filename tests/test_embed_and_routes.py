import base64, json
import pytest
from mutagen.flac import FLAC
from art_manager import artwork, server
from conftest import PNG_1PX, JPEG_BYTES, png_as_jpeg

def _scan(client, root):
    return client.get('/api/scan', query_string={'root': str(root)}).json

def _album(lib, a, al):
    ar = next(x for x in lib['artists'] if x['name'] == a)
    return next(x for x in ar['albums'] if x['name'] == al)

def _run_embed(client, body):
    r = client.post('/api/prepare-embed', json=body)
    assert r.status_code == 200, r.json
    job = r.json
    stream = client.get(f"/api/embed/{job['embed_id']}").get_data(as_text=True)
    events = [json.loads(l[6:]) for l in stream.splitlines() if l.startswith('data: ')]
    assert events[-1]['status'] == 'done'
    assert events[-1]['errors'] == []
    return job

def _pictures(path):
    return [p.data for p in FLAC(str(path)).pictures]

@pytest.mark.parametrize('source', ['upload', 'embedded', 'cover_file', 'url'])
def test_embed_every_source(client, make_library, monkeypatch, source):
    root = make_library()
    lib = _scan(client, root)
    al = _album(lib, '100% Orange Juice', 'Multi')
    body = {'album_path': al['path'], 'disc_info': al['disc_info'], 'source': source}
    expected = png_as_jpeg()   # PNG sources are converted: everything is stored as JPEG
    if source == 'upload':
        body['image_data'] = 'data:image/png;base64,' + base64.b64encode(PNG_1PX).decode()
    elif source == 'embedded':
        al = _album(lib, 'Alpha', 'Second')
        body.update(album_path=al['path'], disc_info=al['disc_info'])
    elif source == 'cover_file':
        body['cover_path'] = al['cover_path']; expected = JPEG_BYTES
    else:
        monkeypatch.setattr(server, 'dl_image', lambda url: (PNG_1PX, 'image/png'))
        body['image_url'] = 'https://example.invalid/x.png'
    job = _run_embed(client, body)
    flacs = artwork.collect_album_flacs(body['disc_info'])
    assert job['total_tracks'] == len(flacs) > 0
    for f in flacs:
        assert _pictures(f) == [expected]

def test_upload_rejects_other_types(client, make_library):
    root = make_library()
    al = _album(_scan(client, root), 'Alpha', 'First')
    r = client.post('/api/prepare-embed', json={'album_path': al['path'], 'disc_info': al['disc_info'],
        'source': 'upload', 'image_data': 'data:image/gif;base64,R0lGOD=='})
    assert r.status_code == 500 and 'Unsupported' in r.json['error']

@pytest.mark.parametrize('name', ['!!!', '100% Orange Juice', 'A/B Testing?', 'Ünïcødé — ok'])
def test_spa_served_for_encoded_names(client, name):
    from urllib.parse import quote
    seg = quote(name, safe='')
    for url in (f'/{seg}/', f'/{seg}/{quote("Album #1", safe="")}/'):
        r = client.get(url)
        assert r.status_code == 200 and b'Art Manager' in r.data, url

def test_no_store_on_images_and_search(client, make_library, monkeypatch):
    root = make_library()
    lib = _scan(client, root)
    al = _album(lib, 'Alpha', 'First')
    r = client.get('/api/image/local', query_string={'path': al['cover_path']})
    assert r.status_code == 200 and 'no-store' in r.headers.get('Cache-Control', '')
