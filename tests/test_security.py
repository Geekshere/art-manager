import base64, json, os, stat, sys
import pytest
from art_manager import config, server

SECRET_PW = 'hunter2-very-secret'
SECRET_KEY = 'fanart-abcdef123456'

def _assert_no_secrets(resp):
    body = resp.get_data(as_text=True)
    assert SECRET_PW not in body and SECRET_KEY not in body
    d = json.loads(body)
    assert not set(d) & set(config.SECRET_KEYS)

def test_config_never_returns_secrets(client):
    r = client.post('/api/config', json={'storage_password': SECRET_PW, 'fanart_key': SECRET_KEY,
                                         'library_root': 'smb://nas.local/Media/Music'})
    _assert_no_secrets(r)
    assert r.json['has_storage_password'] is True and r.json['has_fanart_key'] is True
    r = client.get('/api/config')
    _assert_no_secrets(r)
    assert r.json['has_storage_password'] and r.json['has_fanart_key']

def test_blank_secret_keeps_saved_value(client):
    client.post('/api/config', json={'storage_password': SECRET_PW, 'fanart_key': SECRET_KEY})
    client.post('/api/config', json={'storage_username': 'myuser', 'storage_password': '', 'fanart_key': ''})
    assert config.cfg['storage_password'] == SECRET_PW
    assert config.cfg['fanart_key'] == SECRET_KEY
    assert config.cfg['storage_username'] == 'myuser'

def test_clear_secret(client):
    client.post('/api/config', json={'fanart_key': SECRET_KEY})
    r = client.post('/api/config', json={'clear_fanart_key': True})
    assert r.json['has_fanart_key'] is False and config.cfg['fanart_key'] == ''

@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX file modes')
def test_config_file_is_private(client):
    client.post('/api/config', json={'storage_password': SECRET_PW})
    assert stat.S_IMODE(os.stat(config.CFG_FILE).st_mode) == 0o600

def test_env_secrets_used_but_not_persisted(client, monkeypatch):
    monkeypatch.setenv('ARTMGR_STORAGE_PASSWORD', SECRET_PW)
    config.load_cfg()
    assert config.cfg['storage_password'] == SECRET_PW
    client.post('/api/config', json={'storage_username': 'myuser'})
    with open(config.CFG_FILE) as f:
        assert SECRET_PW not in f.read()
    _assert_no_secrets(client.get('/api/config'))

def test_old_smb_env_names_still_work(client, monkeypatch):
    monkeypatch.setenv('ARTMGR_SMB_PASSWORD', SECRET_PW)
    config.load_cfg()
    assert config.cfg['storage_password'] == SECRET_PW

def test_old_config_migrated(client):
    with open(config.CFG_FILE, 'w') as f:
        json.dump({'library_root': 'smb://bob:p%40ss@nas.local:1445/Media/Music',
                   'smb_username': '', 'smb_password': '', 'searxng_url': 'http://localhost:8080'}, f)
    config.load_cfg()
    c = config.cfg
    assert c['library_root'] == 'smb://nas.local/Media/Music'
    assert (c['storage_username'], c['storage_password'], c['smb_port']) == ('bob', 'p@ss', 1445)
    with open(config.CFG_FILE) as f:
        saved = json.load(f)
    assert 'searxng_url' not in saved and 'smb_password' not in saved

def test_old_smb_credential_fields_migrated(client):
    with open(config.CFG_FILE, 'w') as f:
        json.dump({'library_root': 'smb://nas.local/M', 'smb_username': 'myuser', 'smb_password': 'pw'}, f)
    config.load_cfg()
    assert (config.cfg['storage_username'], config.cfg['storage_password']) == ('myuser', 'pw')

def test_password_required_when_set(client):
    server._auth['password'] = 's3cret'
    assert client.get('/api/config').status_code == 401
    bad = base64.b64encode(b'x:wrong').decode()
    assert client.get('/api/config', headers={'Authorization': 'Basic ' + bad}).status_code == 401
    ok = base64.b64encode(b'anyone:s3cret').decode()
    assert client.get('/api/config', headers={'Authorization': 'Basic ' + ok}).status_code == 200

def test_loopback_rejects_foreign_host_header(client):
    server._auth['loopback_only'] = True
    assert client.get('/api/config', headers={'Host': 'localhost:5000'}).status_code == 200
    assert client.get('/api/config', headers={'Host': '127.0.0.1:5000'}).status_code == 200
    assert client.get('/api/config', headers={'Host': 'evil.example:5000'}).status_code == 403

def test_file_routes_confined_to_library(client, make_library, tmp_path):
    root = make_library()
    client.get('/api/scan', query_string={'root': str(root)})
    outside = tmp_path / 'secret.jpg'
    outside.write_bytes(b'\xff\xd8 not yours')
    assert client.get('/api/image/local', query_string={'path': str(outside)}).status_code == 400
    sneaky = str(root / 'Alpha' / '..' / '..' / 'secret.jpg')
    assert client.get('/api/image/local', query_string={'path': sneaky}).status_code == 400
    assert client.get('/api/image/local', query_string={'path': str(root / 'Alpha' / 'artist.jpg')}).status_code == 200
    r = client.post('/api/set-artist-img', json={'artist_path': str(tmp_path), 'image_data': 'data:image/png;base64,AA=='})
    assert r.status_code == 403
    r = client.post('/api/prepare-embed', json={'album_path': str(tmp_path), 'source': 'upload'})
    assert r.status_code == 403
