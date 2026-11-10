"""Self-update: check, verified download, swap + restart. Uses a fake GitHub release served locally."""
import hashlib, http.server, io, json, os, sys, threading, zipfile
import pytest
from art_manager import __version__, config, updater, server

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='uses a shell-script stand-in for the app')

NEW = '99.0.0'
PROGRAM = f'#!/bin/sh\necho "art-manager {NEW}"\n'.encode()

@pytest.fixture
def release(tmp_path, monkeypatch, client):
    """A local 'GitHub' with a release NEW whose asset is a script printing that version."""
    files = {}
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = files.get(self.path)
            if body is None:
                self.send_response(404); self.end_headers(); return
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{srv.server_port}'
    def publish(asset_bytes, name='art-manager-linux-x64', digest=None, size=None, version=NEW):
        files['/dl/' + name] = asset_bytes
        files['/latest'] = json.dumps({
            'tag_name': 'v' + version, 'body': 'Fixes and features', 'html_url': base + '/rel',
            'assets': [{'name': name, 'size': len(asset_bytes) if size is None else size,
                        'digest': digest or 'sha256:' + hashlib.sha256(asset_bytes).hexdigest(),
                        'browser_download_url': base + '/dl/' + name}]}).encode()
    monkeypatch.setenv('ARTMGR_UPDATE_URL', base + '/latest')
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path / 'data'))
    os.makedirs(config.DATA_DIR)
    monkeypatch.setattr(updater, 'asset_name', lambda: 'art-manager-linux-x64')
    monkeypatch.setattr(updater, 'install_kind', lambda: 'app')
    monkeypatch.setattr(updater, 'state', dict(updater.state, status='idle', latest=None, staged=None, error=None))
    yield publish
    srv.shutdown()

def test_versions():
    assert updater.parse_version('v0.10.2') == (0, 10, 2)
    assert updater.newer('0.10.0', '0.9.9') and not updater.newer('0.5.0', '0.5.0')
    assert updater.newer('v1.0', '0.99.99')

def test_check_finds_newer_release(release):
    release(PROGRAM)
    st = updater.check()
    assert st['latest'] == NEW and st['available'] and st['notes'] == 'Fixes and features'

def test_download_verifies_and_stages(release):
    release(PROGRAM)
    updater.check()
    path = updater.download()
    assert open(path, 'rb').read() == PROGRAM and os.access(path, os.X_OK)
    assert updater.staged_update() == path and updater.public_state()['status'] == 'ready'

@pytest.mark.parametrize('kind', ['digest', 'size', 'version'])
def test_bad_downloads_are_rejected(release, kind):
    if kind == 'digest':
        release(PROGRAM, digest='sha256:' + '0' * 64)
    elif kind == 'size':
        release(PROGRAM, size=len(PROGRAM) + 10)
    else:
        release(b'#!/bin/sh\necho "art-manager 1.2.3"\n')
    updater.check()
    with pytest.raises(RuntimeError):
        updater.download()
    assert updater.staged_update() is None
    assert not os.listdir(os.path.join(config.DATA_DIR, 'update'))   # nothing half-written left
    assert updater.public_state()['error']

def test_macos_zip_is_unpacked(release, monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('art-manager-macos-arm64', PROGRAM)
    monkeypatch.setattr(updater, 'asset_name', lambda: 'art-manager-macos-arm64.zip')
    release(buf.getvalue(), name='art-manager-macos-arm64.zip')
    updater.check()
    assert open(updater.download(), 'rb').read() == PROGRAM

def test_unexpected_download_host_refused(release, monkeypatch):
    release(PROGRAM)
    updater.check()
    monkeypatch.delenv('ARTMGR_UPDATE_URL')
    with pytest.raises(RuntimeError, match='unexpected address'):
        updater.download()

def test_apply_replaces_program_and_restarts(release, tmp_path, monkeypatch):
    release(PROGRAM)
    updater.check()
    staged = updater.download()
    prog = tmp_path / 'app' / 'art-manager'
    prog.parent.mkdir()
    prog.write_bytes(b'old program')
    monkeypatch.setattr(updater, '_program', lambda: str(prog))
    calls = []
    monkeypatch.setattr(updater.os, 'execve', lambda p, a, env: calls.append((p, a, env.get('PYINSTALLER_RESET_ENVIRONMENT'))))
    closed = []
    monkeypatch.setattr(updater.os, 'closerange', lambda lo, hi: closed.append((lo, hi)))
    updater.apply(staged, ['--updated-from', __version__])
    assert prog.read_bytes() == PROGRAM and os.access(prog, os.X_OK)
    assert calls == [(str(prog), [str(prog), '--updated-from', __version__], '1')]   # fresh unpack, not the old one
    assert closed == [(3, 65536)]   # inherited sockets closed before exec (else the port stays busy)

def test_cleanup_removes_old_versions(release, tmp_path, monkeypatch):
    d = os.path.join(config.DATA_DIR, 'update'); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'art-manager-0.0.1'), 'w').close()       # older than us
    open(os.path.join(d, 'art-manager-99.0.0.part'), 'w').close()  # interrupted download
    prog = tmp_path / 'art-manager'; prog.write_text('x')
    (tmp_path / 'art-manager.old-123').write_text('old')
    monkeypatch.setattr(updater, '_program', lambda: str(prog))
    updater.cleanup()
    assert os.listdir(d) == [] and not (tmp_path / 'art-manager.old-123').exists()

def test_restart_args_keep_user_arguments(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['art-manager', '/music', '--updated-from', '0.1.0', '--no-browser'])
    assert server._restart_args(True) == ['/music', '--updated-from', __version__, '--after-update', '--no-browser']
    assert server._restart_args(False) == ['/music', '--updated-from', __version__]

def test_api_for_non_app_installs(client):
    st = client.get('/api/update').json
    assert st['current'] == __version__ and st['kind'] == 'source' and 'git pull' in st['how_to_update']
    r = client.post('/api/update/install', json={})
    assert r.status_code == 400 and 'git pull' in r.json['error']
    assert client.post('/api/update/install', data='x').status_code == 400   # JSON only

def test_update_settings_saved(client):
    r = client.post('/api/config', json={'update_check': False, 'update_auto_install': False}).json
    assert r['update_check'] is False and r['update_auto_install'] is False

def test_staged_file_from_earlier_run_reported_ready(release):
    release(PROGRAM)
    updater.check()
    updater.download()
    # a new process: nothing in memory, but the verified download is on disk
    updater.state.update(status='idle', staged=None)
    st = updater.public_state()
    assert st['status'] == 'ready' and st['staged'].endswith(NEW)
