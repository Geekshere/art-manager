"""Setup and settings conveniences: folder browser, saved access settings, remembered CLI values."""
import json, os, socket
import pytest
from art_manager import config, server

def test_browse_starting_points(client):
    d = client.get('/api/browse').json
    assert d['places'] and d['dirs'][0]['name'] == 'Home folder'

def test_browse_local_folder(client, make_library):
    root = make_library()
    d = client.get('/api/browse', query_string={'path': str(root)}).json
    assert [x['name'] for x in d['dirs']] == ['!!!', '!Playlists', '100% Orange Juice', 'Alpha']
    assert d['parent'] == str(root.parent)
    d = client.get('/api/browse', query_string={'path': str(root / 'Alpha' / 'First')}).json
    assert d['flacs'] == 2   # the hint "this looks like an album"

def test_browse_reports_errors(client, tmp_path):
    d = client.get('/api/browse', query_string={'path': str(tmp_path / 'nope')}).json
    assert 'error' in d

def test_app_password_is_hashed_and_enforced(client):
    r = client.post('/api/config', json={'app_password': 'open sesame'})
    assert r.json['has_app_password_hash'] is True
    body = r.get_data(as_text=True)
    assert 'open sesame' not in body and 'pbkdf2' not in body
    stored = config.cfg['app_password_hash']
    assert stored.startswith('pbkdf2_sha256$') and 'open sesame' not in stored
    assert client.get('/api/config').status_code == 401
    import base64
    auth = lambda pw: {'Authorization': 'Basic ' + base64.b64encode(f'x:{pw}'.encode()).decode()}
    assert client.get('/api/config', headers=auth('wrong')).status_code == 401
    assert client.get('/api/config', headers=auth('open sesame')).status_code == 200
    # cannot be set to an arbitrary hash directly
    client.post('/api/config', headers=auth('open sesame'), json={'app_password_hash': 'x'})
    assert config.cfg['app_password_hash'] == stored
    client.post('/api/config', headers=auth('open sesame'), json={'clear_app_password': True})
    assert client.get('/api/config').status_code == 200

def test_access_settings_saved(client):
    r = client.post('/api/config', json={'allow_network': True, 'port': 5123}).json
    assert r['allow_network'] is True and r['port'] == 5123
    with open(config.CFG_FILE) as f:
        saved = json.load(f)
    assert saved['allow_network'] is True and saved['port'] == 5123
    client.post('/api/config', json={'port': 99999})
    assert config.cfg['port'] == 5123   # out of range ignored

def test_command_line_values_are_remembered(client, monkeypatch):
    monkeypatch.delenv('ARTMGR_HOST', raising=False); monkeypatch.delenv('ARTMGR_PORT', raising=False)
    with socket.socket() as t:
        t.bind(('127.0.0.1', 0)); free = t.getsockname()[1]
    host, port, wanted, _ = server.prepare(['smb://nas.local/Media/Music', '--user', 'myuser',
                                            '--pass', 'pw', '--port', str(free), '--no-browser'])
    with open(config.CFG_FILE) as f:
        saved = json.load(f)
    assert saved['library_root'] == 'smb://nas.local/Media/Music'
    assert saved['storage_username'] == 'myuser' and saved['storage_password'] == 'pw'
    assert host == '127.0.0.1'   # default: this computer only
    # next start without arguments uses the saved values
    config.cfg.clear(); config.cfg.update(config.DEFAULTS)
    server.prepare(['--no-browser'])
    assert config.cfg['library_root'] == 'smb://nas.local/Media/Music'

def test_saved_allow_network_used_and_busy_port_skipped(client, monkeypatch):
    monkeypatch.delenv('ARTMGR_HOST', raising=False); monkeypatch.delenv('ARTMGR_PORT', raising=False)
    with socket.socket() as busy:
        busy.bind(('127.0.0.1', 0)); busy.listen()
        taken = busy.getsockname()[1]
        client.post('/api/config', json={'port': taken})
        host, port, wanted, _ = server.prepare(['--no-browser', '--host', '127.0.0.1'])
        assert wanted == taken and port != taken   # moved to a free port
    client.post('/api/config', json={'allow_network': True})
    host, *_ = server.prepare(['--no-browser'])
    assert host == '0.0.0.0'


def test_bad_port_rejected_clearly(client, capsys):
    with pytest.raises(SystemExit):
        server.prepare(['--port', '70000'])
    assert 'between 1 and 65535' in capsys.readouterr().err

def test_ping_needs_no_password(client):
    server._auth['password'] = 'secret'
    assert client.get('/api/config').status_code == 401
    r = client.get('/api/ping')
    assert r.status_code == 200 and r.json['app'] == 'art-manager'

def test_quit_requires_json_and_exits(client, monkeypatch):
    calls = []
    monkeypatch.setattr(server.threading, 'Timer', lambda t, fn: type('T', (), {'start': lambda self: calls.append(t)})())
    assert client.post('/api/quit', data='x').status_code == 400   # plain form post: refused
    assert calls == []
    assert client.post('/api/quit', json={}).json == {'ok': True}
    assert calls == [0.5]

def test_detects_running_instance(client):
    """A second launch finds the first one through /api/ping."""
    import threading
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, server.app)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    try:
        assert server._art_manager_at(srv.server_port) is True
    finally:
        srv.shutdown()
    with socket.socket() as s2:
        s2.bind(('127.0.0.1', 0)); s2.listen()
        assert server._art_manager_at(s2.getsockname()[1]) is False   # something else on that port

def test_frozen_linux_child_env_restored(monkeypatch):
    monkeypatch.setattr(server.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(server.sys, 'platform', 'linux')
    monkeypatch.setenv('LD_LIBRARY_PATH', '/tmp/_MEIxyz')
    monkeypatch.setenv('LD_LIBRARY_PATH_ORIG', '/usr/local/lib')
    server._fix_child_env()
    assert os.environ['LD_LIBRARY_PATH'] == '/usr/local/lib'
    monkeypatch.setenv('LD_LIBRARY_PATH', '/tmp/_MEIxyz')
    server._fix_child_env()                      # no original value: remove it entirely
    assert 'LD_LIBRARY_PATH' not in os.environ
