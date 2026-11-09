"""Unit tests for storage backends that don't need a server."""
import io
import pytest
from art_manager import storage
from art_manager.storage import SMBStorage, WebDAVStorage, Entry

NEXTCLOUD_PROPFIND = b'''<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns">
 <d:response><d:href>/remote.php/dav/files/myuser/Music/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
   <d:getlastmodified>Mon, 02 Nov 2026 10:00:00 GMT</d:getlastmodified><d:getetag>"root1"</d:getetag></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/files/myuser/Music/100%25%20Orange%20Juice/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
   <d:getlastmodified>Mon, 02 Nov 2026 10:00:00 GMT</d:getlastmodified><d:getetag>"abc123"</d:getetag></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  <d:propstat><d:prop><d:getcontentlength/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>
 <d:response><d:href>https://cloud.example.com/remote.php/dav/files/myuser/Music/Album%20%231/</d:href>
  <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
   <d:getlastmodified>Mon, 02 Nov 2026 10:00:00 GMT</d:getlastmodified></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/files/myuser/Music/cover.jpg</d:href>
  <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>1234</d:getcontentlength>
   <d:getlastmodified>Mon, 02 Nov 2026 10:00:00 GMT</d:getlastmodified><d:getetag>"f1"</d:getetag></d:prop>
   <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>'''

class FakeResp:
    def __init__(self, status, content=b'', headers=None):
        self.status_code, self.content, self.headers = status, content, headers or {}

class FakeSession:
    def __init__(self, handler):
        self.handler, self.requests = handler, []
        self.auth, self.headers = None, {}
    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return self.handler(method, url, kw)

def dav_with(handler):
    d = WebDAVStorage()
    sess = FakeSession(handler)
    d._session = lambda: sess
    return d, sess

def test_propfind_parsing_etags_and_names():
    d, sess = dav_with(lambda m, u, kw: FakeResp(207, NEXTCLOUD_PROPFIND))
    entries = {e.name: e for e in d.scandir('webdavs://cloud.example.com/remote.php/dav/files/myuser/Music')}
    assert set(entries) == {'100% Orange Juice', 'Album #1', 'cover.jpg'}   # the folder itself skipped
    oj = entries['100% Orange Juice']
    assert oj.is_dir and oj.fingerprint == 'etag:abc123'
    # collection without an ETag: its mtime is NOT trusted -> no fingerprint -> always rescanned
    assert entries['Album #1'].fingerprint is None
    f = entries['cover.jpg']
    assert not f.is_dir and f.size == 1234 and f.mtime is not None
    method, url, kw = sess.requests[0]
    assert method == 'PROPFIND' and kw['headers']['Depth'] == '1'
    assert url == 'https://cloud.example.com/remote.php/dav/files/myuser/Music'

def test_url_quoting_keeps_special_names():
    assert WebDAVStorage._url('webdav://h:8080/Music/100% Orange Juice/Album #1/01?.flac') == \
        'http://h:8080/Music/100%25%20Orange%20Juice/Album%20%231/01%3F.flac'
    assert WebDAVStorage._url('https://h/a b') == 'https://h/a%20b'

def test_smb_path_split_keeps_special_names():
    host, port, unc = SMBStorage()._split('smb://nas.local:1445/Media/Music/Album #1/Why?')
    assert (host, port, unc) == ('nas.local', 1445, r'\\nas.local\Media\Music\Album #1\Why?')

def test_status_codes_map_to_exceptions():
    for code, exc in ((404, FileNotFoundError), (401, PermissionError), (403, PermissionError)):
        d, _ = dav_with(lambda m, u, kw, c=code: FakeResp(c))
        with pytest.raises(exc):
            d.scandir('webdav://h/x')
    assert storage.is_permanent(PermissionError()) and not storage.is_permanent(TimeoutError())

def test_range_reader_and_fallback_when_range_ignored():
    blob = bytes(range(256)) * 4096   # 1 MiB
    stat_xml = (b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/f.flac</d:href><d:propstat><d:prop>'
                b'<d:resourcetype/><d:getcontentlength>%d</d:getcontentlength></d:prop>'
                b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>' % len(blob))
    for honours_range in (True, False):
        def h(m, u, kw):
            if m == 'PROPFIND': return FakeResp(207, stat_xml)
            rng = kw.get('headers', {}).get('Range')
            if rng and honours_range:
                a, b = map(int, rng[6:].split('-'))
                return FakeResp(206, blob[a:b + 1])
            return FakeResp(200, blob)
        d, sess = dav_with(h)
        with d.open('webdav://h/f.flac', 'rb') as f:
            assert f.read(4) == blob[:4]
            f.seek(700_000)
            assert f.read(10) == blob[700_000:700_010]
        gets = [r for r in sess.requests if r[0] == 'GET']
        if honours_range:
            assert all('Range' in r[2]['headers'] for r in gets) and len(gets) == 2   # never the whole file
        else:
            assert len(gets) == 1   # whole body fetched once, then served from memory

def test_upload_goes_via_temp_and_move_and_is_verified():
    stored = {}
    def h(m, u, kw):
        if m == 'PUT':
            stored[u] = kw['data']; return FakeResp(201)
        if m == 'MOVE':
            stored[kw['headers']['Destination']] = stored.pop(u); return FakeResp(201)
        if m == 'PROPFIND':
            size = len(stored.get(u, b'')) - 1    # server lies: one byte short
            return FakeResp(207, (b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/x</d:href><d:propstat><d:prop>'
                                  b'<d:resourcetype/><d:getcontentlength>%d</d:getcontentlength></d:prop>'
                                  b'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>') % size)
        return FakeResp(204)
    d, sess = dav_with(h)
    with pytest.raises(storage.WebDAVError, match='could not be verified'):
        d.upload('webdav://h/dir/cover.jpg', b'12345')
    methods = [r[0] for r in sess.requests]
    assert methods[:2] == ['PUT', 'MOVE']
    assert '.artmgr-tmp-' in sess.requests[0][1] and sess.requests[0][1].startswith('http://h/dir/')

def test_failed_edit_is_not_uploaded():
    d, sess = dav_with(lambda m, u, kw: FakeResp(200, b'original'))
    with pytest.raises(RuntimeError):
        with d.open('webdav://h/f.flac', 'rb+') as f:
            f.write(b'half-')
            raise RuntimeError('mutagen failed')
    assert [r[0] for r in sess.requests] == ['GET']   # no PUT

def test_scheme_dispatch():
    assert storage.kind('/music') == 'local' and storage.kind(r'C:\Music') == 'local'
    assert storage.kind('file:///music') == 'local'
    assert storage.kind('smb://nas.local/M') == 'smb'
    for u in ('webdav://h/x', 'webdavs://h/x', 'https://h/x', 'http://h/x'):
        assert storage.kind(u) == 'webdav'
