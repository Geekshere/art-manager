"""
The same library through every storage backend: local, SMB (a real smbd on a
random port) and WebDAV (wsgidav). Each backend must scan identically, embed
correctly and pick up changes on rescan.

SMB needs the `smbd` binary and root (directly or via passwordless sudo);
WebDAV needs the `wsgidav` and `cheroot` packages. Missing pieces skip.
"""
import getpass, os, shutil, socket, subprocess, threading, time
import pytest
from mutagen.flac import FLAC

from art_manager import storage
from conftest import add_flac, build_library, JPEG_BYTES, PNG_1PX, png_as_jpeg

USER, PASSWORD = getpass.getuser(), 'Test-pw-123'


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def _wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False

def _library(tmp_path, flac):
    root = build_library(tmp_path / 'share' / 'Music', flac)
    add_flac(root / 'Alpha' / 'Album #1' / '01.flac', flac)   # '#' must survive URLs
    return root


# ── servers ───────────────────────────────────────────────────────────────────

def _smbd_prefix():
    smbd = shutil.which('smbd') or ('/usr/sbin/smbd' if os.path.exists('/usr/sbin/smbd') else None)
    if not smbd:
        pytest.skip('smbd not installed')
    if os.geteuid() == 0:
        return [], smbd
    if shutil.which('sudo') and subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode == 0:
        return ['sudo', '-n'], smbd
    pytest.skip('smbd needs root (or passwordless sudo)')

@pytest.fixture
def smb_root(tmp_path, flac_bytes, client):
    prefix, smbd = _smbd_prefix()
    root = _library(tmp_path, flac_bytes)
    d = tmp_path / 'samba'
    for sub in ('priv', 'lock', 'state', 'cache', 'pid', 'log'):
        (d / sub).mkdir(parents=True)
    port = _free_port()
    conf = d / 'smb.conf'
    conf.write_text(f"""[global]
  smb ports = {port}
  bind interfaces only = yes
  interfaces = lo
  private dir = {d}/priv
  lock directory = {d}/lock
  state directory = {d}/state
  cache directory = {d}/cache
  pid directory = {d}/pid
  ncalrpc dir = {d}/state/ncalrpc
  log file = {d}/log/log.%m
  passdb backend = tdbsam:{d}/priv/passdb.tdb
  load printers = no
  disable spoolss = yes
  server role = standalone server
  server min protocol = SMB2_10
[media]
  path = {tmp_path / 'share'}
  read only = no
  force user = {USER}
""")
    smbpasswd = shutil.which('smbpasswd') or '/usr/bin/smbpasswd'
    subprocess.run(prefix + [smbpasswd, '-c', str(conf), '-a', '-s', USER],
                   input=f'{PASSWORD}\n{PASSWORD}\n', text=True, check=True, capture_output=True)
    # Own session: on shutdown smbd SIGTERMs its whole process group, which
    # must not be pytest's.
    proc = subprocess.Popen(prefix + [smbd, '-F', '--no-process-group', '-s', str(conf), '--debug-stdout'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        if not _wait_port(port):
            pytest.skip('smbd did not start')
        client.post('/api/config', json={'storage_username': USER, 'storage_password': PASSWORD,
                                         'smb_port': port})
        yield root, 'smb://127.0.0.1/media/Music'
    finally:
        storage.reset_all()
        # smbd forks a child per connection: stop the whole group
        subprocess.run(prefix + ['kill', '-TERM', '--', f'-{proc.pid}'], capture_output=True)
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            subprocess.run(prefix + ['kill', '-KILL', '--', f'-{proc.pid}'], capture_output=True)

@pytest.fixture
def webdav_root(tmp_path, flac_bytes, client):
    wsgidav = pytest.importorskip('wsgidav.wsgidav_app')
    cheroot_wsgi = pytest.importorskip('cheroot.wsgi')
    root = _library(tmp_path, flac_bytes)
    app = wsgidav.WsgiDAVApp({
        'provider_mapping': {'/dav': str(tmp_path / 'share')},
        'simple_dc': {'user_mapping': {'*': {USER: {'password': PASSWORD}}}},
        'http_authenticator': {'domain_controller': None, 'accept_basic': True,
                               'accept_digest': False, 'default_to_digest': False},
        'verbose': 0, 'logging': {'enable': False}, 'lock_storage': True,
    })
    port = _free_port()
    srv = cheroot_wsgi.Server(('127.0.0.1', port), app, numthreads=8)
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    try:
        assert _wait_port(port)
        client.post('/api/config', json={'storage_username': USER, 'storage_password': PASSWORD})
        yield root, f'webdav://127.0.0.1:{port}/dav/Music'
    finally:
        srv.stop()
        storage.reset_all()

@pytest.fixture
def local_root(tmp_path, flac_bytes):
    root = _library(tmp_path, flac_bytes)
    return root, str(root)

@pytest.fixture(params=['local', 'smb', 'webdav'])
def backend(request):
    return request.getfixturevalue(f'{request.param}_root')


# ── helpers ───────────────────────────────────────────────────────────────────

def _scan(client, url):
    r = client.get('/api/scan', query_string={'root': url})
    assert r.status_code == 200, r.json
    assert r.json['errors'] == [], r.json['errors']
    return r.json

def _shape(lib):
    """Everything about a scan except the paths (which differ per backend)."""
    return sorted(
        (a['name'], a['has_img'], sorted(
            (al['name'], al['is_multi_disc'], al['total_tracks'], al['has_cover'], al['cover_in_root'],
             tuple((d['name'], d['flac_count'], d['has_cover']) for d in al['disc_info']))
            for al in a['albums']))
        for a in lib['artists'])

def _album(lib, a, al):
    ar = next(x for x in lib['artists'] if x['name'] == a)
    return next(x for x in ar['albums'] if x['name'] == al)

def _embed(client, al, **body):
    r = client.post('/api/prepare-embed', json={'album_path': al['path'], 'disc_info': al['disc_info'], **body})
    assert r.status_code == 200, r.json
    out = client.get(f"/api/embed/{r.json['embed_id']}").get_data(as_text=True)
    assert '"status": "done", "errors": []' in out, out
    return r.json


# ── tests ─────────────────────────────────────────────────────────────────────

def test_scans_identically_to_local(client, backend, tmp_path):
    root, url = backend
    expected = _shape(_scan(client, str(root)))
    assert _shape(_scan(client, url)) == expected
    assert ('Album #1', False, 1, False, False, ((None, 1, False),)) in dict(
        (a[0], a[2]) for a in expected)['Alpha']

def test_embed_through_backend(client, backend):
    root, url = backend
    lib = _scan(client, url)
    al = _album(lib, '100% Orange Juice', 'Multi')
    job = _embed(client, al, source='upload',
                 image_data='data:image/png;base64,' + __import__('base64').b64encode(PNG_1PX).decode())
    assert job['total_tracks'] == 2
    jpeg = png_as_jpeg()
    disk = root / '100% Orange Juice' / 'Multi'
    for f in (disk / 'CD 1' / '01.flac', disk / 'CD 2' / '01.flac'):
        pics = FLAC(str(f)).pictures                 # verified on disk, not via the backend
        assert [(p.mime, p.data) for p in pics] == [('image/jpeg', jpeg)]
    # cover.jpg in the album folder AND in every disc folder; no stray PNG
    for d in (disk, disk / 'CD 1', disk / 'CD 2'):
        assert (d / 'cover.jpg').read_bytes() == jpeg, d
    assert not list(disk.rglob('cover.png'))
    lib = _scan(client, url)
    m = _album(lib, '100% Orange Juice', 'Multi')
    assert m['has_cover'] and m['cover_in_root'] and all(x['has_cover'] for x in m['disc_info'])
    assert not [p for p in disk.rglob('.artmgr-tmp-*')], 'temporary upload left behind'
    # read back through the backend: the embedded art and the new cover
    first = f"{url}/100% Orange Juice/Multi/CD 1/01.flac"
    r = client.get('/api/track-art', query_string={'file': first})
    assert r.status_code == 200 and r.data == jpeg
    r = client.get('/api/image/local', query_string={'path': job['cover_path']})
    assert r.status_code == 200 and r.data == jpeg
    assert sorted(x['path'] for x in job['disc_covers']) == sorted(x['path'] for x in al['disc_info'])

def test_embedded_and_cover_file_sources(client, backend):
    root, url = backend
    lib = _scan(client, url)
    second = _album(lib, 'Alpha', 'Second')              # has embedded art only
    r = client.get('/api/image/embedded', query_string={'path': second['path'],
                   'disc_info': __import__('json').dumps(second['disc_info'])})
    assert r.json['has_art'] is True
    _embed(client, second, source='embedded')
    assert (root / 'Alpha' / 'Second' / 'cover.jpg').read_bytes() == png_as_jpeg()
    first = _album(lib, 'Alpha', 'First')                # has cover.jpg
    _embed(client, first, source='cover_file', cover_path=first['cover_path'])
    for f in (root / 'Alpha' / 'First').glob('*.flac'):
        assert [p.data for p in FLAC(str(f)).pictures] == [JPEG_BYTES]

def test_rescan_picks_up_changes(client, backend, flac_bytes):
    root, url = backend
    _scan(client, url)
    (root / '100% Orange Juice' / 'Multi' / 'CD 2' / 'cover.jpg').write_bytes(JPEG_BYTES)
    t = time.time() + 5
    os.utime(root / '100% Orange Juice' / 'Multi' / 'CD 2', (t, t))
    add_flac(root / 'Newcomer' / 'Debut' / '01.flac', flac_bytes)
    lib = _scan(client, url)
    assert [d['has_cover'] for d in _album(lib, '100% Orange Juice', 'Multi')['disc_info']] == [True, True]
    assert any(a['name'] == 'Newcomer' for a in lib['artists'])

def test_tracks_listing_through_backend(client, backend):
    root, url = backend
    lib = _scan(client, url)
    al = _album(lib, '100% Orange Juice', 'Multi')
    r = client.get('/api/album-tracks', query_string={'path': al['path']})
    assert r.status_code == 200
    assert [g['disc_name'] for g in r.json['groups']] == ['CD 1', 'CD 2'] and r.json['total'] == 2

def test_connection_test_endpoint(client, backend):
    _, url = backend
    r = client.get('/api/test-connection', query_string={'root': url}).json
    assert r['ok'] is True, r
    assert '4 folder' in r['msg']   # Alpha, !!!, 100% Orange Juice, !Playlists

def test_bad_credentials_reported(client, backend):
    _, url = backend
    if storage.kind(url) == 'local':
        pytest.skip('no credentials for local folders')
    client.post('/api/config', json={'storage_password': 'wrong'})
    r = client.get('/api/test-connection', query_string={'root': url}).json
    assert r['ok'] is False
    assert 'denied' in r['msg'].lower() or 'authentication' in r['msg'].lower(), r['msg']
