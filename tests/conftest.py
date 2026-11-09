"""Shared fixtures: a generated fake music library, an isolated app client, a fake storage backend."""
import base64, os, shutil, subprocess, sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from art_manager import config, scan, server, storage  # noqa: E402

PNG_1PX = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
JPEG_BYTES = b'\xff\xd8\xff\xe0' + b'\x00' * 64 + b'\xff\xd9'

_TEMPLATE_FLAC = None

def _template_flac(tmp_path_factory):
    """One silent 1 s FLAC made with ffmpeg (once per session), copied everywhere."""
    global _TEMPLATE_FLAC
    if _TEMPLATE_FLAC is None:
        if not shutil.which('ffmpeg'):
            pytest.skip('ffmpeg is required to build FLAC fixtures')
        p = tmp_path_factory.mktemp('tmpl') / 'template.flac'
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'anullsrc=r=44100:cl=mono', '-t', '1', '-c:a', 'flac', str(p)], check=True)
        _TEMPLATE_FLAC = p.read_bytes()
    return _TEMPLATE_FLAC

def add_flac(path, data, picture=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if picture is not None:
        from mutagen.flac import FLAC, Picture
        pic = Picture(); pic.type = 3; pic.mime = 'image/png'; pic.data = picture
        f = FLAC(str(path)); f.add_picture(pic); f.save()

def build_library(root, flac):
    """
      Music/
        Alpha/              artist.jpg; First (cover.jpg, 2 tracks); Second (embedded art only)
        !!!/                Thr!!!er (nothing)
        100% Orange Juice/  Multi: CD 1 (cover.jpg), CD 2 (none)
        !Playlists/         Smart (one flac) — to be ignored
    """
    a = root / 'Alpha'
    a.mkdir(parents=True)
    (a / 'artist.jpg').write_bytes(JPEG_BYTES)
    add_flac(a / 'First' / '01 - One.flac', flac)
    add_flac(a / 'First' / '02 - Two.flac', flac)
    (a / 'First' / 'cover.jpg').write_bytes(JPEG_BYTES)
    add_flac(a / 'Second' / '01 - A.flac', flac, picture=PNG_1PX)
    add_flac(root / '!!!' / 'Thr!!!er' / '01.flac', flac)
    m = root / '100% Orange Juice' / 'Multi'
    add_flac(m / 'CD 1' / '01.flac', flac)
    (m / 'CD 1' / 'cover.jpg').write_bytes(JPEG_BYTES)
    add_flac(m / 'CD 2' / '01.flac', flac)
    add_flac(root / '!Playlists' / 'Smart' / 'x.flac', flac)
    return root

@pytest.fixture
def flac_bytes(tmp_path_factory):
    return _template_flac(tmp_path_factory)

@pytest.fixture
def make_library(tmp_path, flac_bytes):
    return lambda: build_library(tmp_path / 'Music', flac_bytes)


class FakeStorage(storage.LocalStorage):
    """
    'fake:///abs/path' -> the local path, with a log of every listing and
    fault injection: fail[path] = exception, or a callable returning one (or None).
    """
    name = 'fake'

    def __init__(self):
        self.calls = []
        self.fail = {}

    def _p(self, path):
        return path[len('fake://'):]

    def scandir(self, path):
        self.calls.append(path)
        f = self.fail.get(path)
        exc = f() if callable(f) else f
        if exc:
            raise exc
        return super().scandir(path)

    join = storage.Storage.join
    basename = storage.Storage.basename
    dirname = storage.Storage.dirname

@pytest.fixture
def fake():
    fs = FakeStorage()
    storage.register('fake', fs)
    yield fs

def fail_once(exc):
    state = {'done': False}
    def f():
        if state['done']:
            return None
        state['done'] = True
        return exc
    return f


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with a clean config and config/cache files in tmp_path."""
    monkeypatch.setattr(config, 'CFG_FILE', str(tmp_path / 'config.json'))
    monkeypatch.setattr(config, 'CACHE_FILE', str(tmp_path / 'library_cache.json'))
    saved = dict(config.cfg)
    config.cfg.clear(); config.cfg.update(config.DEFAULTS)
    config._env_cfg.clear()
    monkeypatch.setattr(server, '_cached_lib', None)
    monkeypatch.setattr(server, '_auth', {'password': None, 'loopback_only': False})
    monkeypatch.setattr(scan, 'LISTDIR_RETRY_DELAYS', (0, 0))
    storage.reset_all()
    server.app.config['TESTING'] = True
    with server.app.test_client() as c:
        yield c
    config.cfg.clear(); config.cfg.update(saved)
    storage.reset_all()
