"""
Storage backends. Every file access in the app goes through the module-level
helpers at the bottom (scandir, stat, open_file, join, ...), which pick a
backend from the path's scheme:

    /music, C:\\Music, file:///music    LocalStorage
    smb://host[:port]/share/path        SMBStorage
    webdav://host/path  (= http)        WebDAVStorage
    webdavs://host/path (= https), and plain http(s)://host/path

Paths are kept as these human-readable strings everywhere (names are NOT
percent-encoded) and only translated to UNC paths / encoded URLs inside the
backend, right before the network call. Never run urlsplit() on a full path:
folder names may contain '#', '?' or '%'.

Credentials come from cfg['storage_username'] / cfg['storage_password'] —
never from the URL.
"""
import errno, io, os, posixpath, re, stat as stat_mod, threading, uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit

import requests

from .config import cfg

try:
    import smbclient
    _HAVE_SMB = True
except ImportError:   # pragma: no cover - smbprotocol is in requirements
    _HAVE_SMB = False


@dataclass
class Entry:
    name: str
    is_dir: bool
    mtime: float = None      # seconds since epoch, or None if the server doesn't say
    size: int = None
    etag: str = None

    @property
    def fingerprint(self):
        """A value that changes when the directory's contents change, or None if unknown."""
        if self.etag:
            return 'etag:' + self.etag
        if self.mtime is not None:
            return f'mtime:{self.mtime:.6f}'
        return None


class Storage:
    """Interface. Paths are full strings including any scheme."""
    name = 'local'
    # How many listings to run at once. Network backends wait on latency, so
    # parallel requests help; local disks answer in microseconds and Python
    # threads only add lock contention (8 threads scanned ~9x slower than 1).
    parallel = 8

    def scandir(self, path):           # -> [Entry]   ONE round-trip, raises on error
        raise NotImplementedError
    def stat(self, path):              # -> Entry, raises FileNotFoundError
        raise NotImplementedError
    def open(self, path, mode='rb'):   # 'rb', 'wb', 'rb+'
        raise NotImplementedError
    def remove(self, path):            # delete one file
        raise NotImplementedError

    def join(self, base, *parts):
        out = base.rstrip('/')
        for p in parts:
            out += '/' + str(p).strip('/\\')
        return out
    def basename(self, path):
        return path.rstrip('/').rsplit('/', 1)[-1]
    def dirname(self, path):
        return path.rstrip('/').rsplit('/', 1)[0]

    def fingerprint(self, path):
        try:
            return self.stat(path).fingerprint
        except Exception:
            return None
    def reset(self):
        """Drop cached sessions/credentials (settings changed)."""
    def error_message(self, e, path):
        return f'{type(e).__name__}: {e}'


# ── Local ─────────────────────────────────────────────────────────────────────

class LocalStorage(Storage):
    name = 'local'
    parallel = 1

    @staticmethod
    def _p(path):
        if path.lower().startswith('file://'):
            path = unquote(path[7:])
            if re.match(r'^/[A-Za-z]:', path):   # file:///C:/Music
                path = path[1:]
        return path

    def scandir(self, path):
        out = []
        with os.scandir(self._p(path)) as it:
            for e in it:
                try:
                    is_dir = e.is_dir()
                    if not is_dir and not e.is_file():
                        continue
                    # On Windows, DirEntry.stat() comes from the parent's
                    # directory index, which NTFS updates lazily — a folder's
                    # mtime there can be stale. Stat folders directly.
                    st = os.stat(e.path) if (is_dir and os.name == 'nt') else e.stat()
                except OSError:
                    continue   # broken symlink etc.
                out.append(Entry(e.name, is_dir, st.st_mtime, st.st_size))
        return out

    def stat(self, path):
        st = os.stat(self._p(path))
        return Entry(os.path.basename(self._p(path).rstrip('/\\')), stat_mod.S_ISDIR(st.st_mode),
                     st.st_mtime, st.st_size)

    def open(self, path, mode='rb'):
        return open(self._p(path), mode)

    def remove(self, path):
        os.remove(self._p(path))

    def join(self, base, *parts):
        if base.lower().startswith('file://'):
            return super().join(base, *parts)
        return os.path.join(base, *[str(p) for p in parts])

    def basename(self, path):
        return os.path.basename(self._p(path).rstrip('/\\'))

    def dirname(self, path):
        if path.lower().startswith('file://'):
            return super().dirname(path)
        return os.path.dirname(path.rstrip('/\\'))


# ── SMB ───────────────────────────────────────────────────────────────────────

class SMBStorage(Storage):
    """
    smb://host[:port]/share/path via smbprotocol's smbclient, which reads and
    writes in place (seekable handles), so mutagen edits FLACs directly.

    Gotchas:
      - smbclient pools connections by "server:port". The port must be passed
        to EVERY call, or it silently opens a new session on 445.
      - register_session() has an unguarded check-then-set race; first
        registration per server is serialized by _lock.
      - scandir entries carry timestamps/sizes from the directory query; never
        call entry.stat() (a network round-trip per entry).
    """
    name = 'smb'

    def __init__(self):
        self._ready = set()
        self._lock = threading.Lock()

    def _split(self, path):
        """'smb://host[:port]/share/a/b' -> (host, port, r'\\\\host\\share\\a\\b')"""
        rest = path[6:]
        netloc, _, sub = rest.partition('/')
        host, _, port = netloc.partition(':')
        port = int(port) if port else int(cfg.get('smb_port') or 445)
        unc = '\\\\' + host + (('\\' + sub.strip('/').replace('/', '\\')) if sub.strip('/') else '')
        return host, port, unc

    def _prep(self, path):
        if not _HAVE_SMB:
            raise RuntimeError("SMB support requires the 'smbprotocol' package — run: pip install smbprotocol")
        host, port, unc = self._split(path)
        key = f'{host}:{port}'
        if key not in self._ready:
            with self._lock:
                if key not in self._ready:
                    smbclient.register_session(
                        host, port=port,
                        username=cfg.get('storage_username') or None,
                        password=cfg.get('storage_password') or None,
                        connection_timeout=15)
                    self._ready.add(key)
        return unc, port

    def scandir(self, path):
        unc, port = self._prep(path)
        out = []
        for e in smbclient.scandir(unc, port=port):
            info = e.smb_info
            is_dir = bool(info.file_attributes & 0x10)   # FILE_ATTRIBUTE_DIRECTORY
            out.append(Entry(e.name, is_dir, info.last_write_time.timestamp(),
                             None if is_dir else info.end_of_file))
        return out

    def stat(self, path):
        unc, port = self._prep(path)
        st = smbclient.stat(unc, port=port)
        return Entry(self.basename(path), stat_mod.S_ISDIR(st.st_mode), st.st_mtime, st.st_size)

    def open(self, path, mode='rb'):
        unc, port = self._prep(path)
        return smbclient.open_file(unc, mode=mode, port=port)

    def remove(self, path):
        unc, port = self._prep(path)
        smbclient.remove(unc, port=port)

    def reset(self):
        with self._lock:
            self._ready = set()
        if _HAVE_SMB:
            try: smbclient.reset_connection_cache()
            except Exception: pass

    def error_message(self, e, path):
        low = str(e).lower()
        if any(k in low for k in ('logon', 'authentication', 'access_denied', 'access is denied')):
            return f"SMB authentication failed for {path!r} — check the username/password in Settings."
        if 'bad_network_name' in low or 'network name' in low:
            return f"SMB share not found: {path!r} — check the server and share name."
        if 'name_not_found' in low or 'path_not_found' in low or isinstance(e, FileNotFoundError):
            return f"Path not found on the SMB share: {path!r}"
        if any(k in low for k in ('timed out', 'timeout', 'refused', 'unreachable', 'getaddrinfo',
                                   'name or service not known')):
            return f"Can't reach the SMB server for {path!r} — check the host/IP and that the share is online."
        return f"SMB error accessing {path!r}: {e}"


# ── WebDAV ────────────────────────────────────────────────────────────────────

_DAV = '{DAV:}'
_PROPFIND_BODY = (b'<?xml version="1.0" encoding="utf-8"?>'
                  b'<d:propfind xmlns:d="DAV:"><d:prop>'
                  b'<d:resourcetype/><d:getlastmodified/><d:getcontentlength/><d:getetag/>'
                  b'</d:prop></d:propfind>')


class WebDAVError(OSError):
    pass


class WebDAVStorage(Storage):
    """
    WebDAV over plain `requests`:
      - listing: one PROPFIND Depth:1 per directory (type, mtime, size, ETag)
      - reads:   'rb' is a lazy HTTP Range reader, so reading FLAC metadata
                 doesn't download whole tracks
      - writes:  no random access over HTTP, so 'rb+' downloads the file,
                 lets the caller (mutagen) edit it in memory, then on close
                 uploads to a temporary name, MOVEs it over the original and
                 verifies the size. 'wb' uploads the same way.
      - fingerprints: directory ETags when the server provides them (Nextcloud
        etc. change them when anything inside changes). Collection mtimes are
        NOT trusted — many servers never update them — so without an ETag a
        directory's fingerprint is None and it is rescanned every time.
    """
    name = 'webdav'
    TIMEOUT = 30

    def __init__(self):
        self._local = threading.local()
        self._gen = 0

    def _session(self):
        s = getattr(self._local, 's', None)
        if s is None or getattr(self._local, 'gen', -1) != self._gen:
            s = requests.Session()
            user, pw = cfg.get('storage_username'), cfg.get('storage_password')
            if user or pw:
                s.auth = (user or '', pw or '')
            s.headers['User-Agent'] = 'ArtManager'
            self._local.s, self._local.gen = s, self._gen
        return s

    def reset(self):
        self._gen += 1

    @staticmethod
    def _url(path):
        scheme, _, rest = path.partition('://')
        netloc, _, sub = rest.partition('/')
        http = {'webdav': 'http', 'webdavs': 'https'}.get(scheme.lower(), scheme.lower())
        return f'{http}://{netloc}/' + quote(sub, safe='/')

    def _req(self, method, path, ok=(200, 201, 204, 207), **kw):
        kw.setdefault('timeout', self.TIMEOUT)
        r = self._session().request(method, self._url(path), **kw)
        if r.status_code in ok:
            return r
        if r.status_code == 404:
            raise FileNotFoundError(f'{path}: not found (404)')
        if r.status_code in (401, 403):
            raise PermissionError(f'{path}: access denied ({r.status_code})')
        raise WebDAVError(f'{method} {path}: HTTP {r.status_code}')

    def _propfind(self, path, depth):
        r = self._req('PROPFIND', path, ok=(207,), data=_PROPFIND_BODY,
                      headers={'Depth': str(depth), 'Content-Type': 'application/xml; charset=utf-8'})
        out = []
        for resp in ET.fromstring(r.content).iter(_DAV + 'response'):
            href = resp.findtext(_DAV + 'href') or ''
            props = {}
            for ps in resp.iter(_DAV + 'propstat'):
                if '200' not in (ps.findtext(_DAV + 'status') or '200'):
                    continue
                prop = ps.find(_DAV + 'prop')
                if prop is not None:
                    for child in prop:
                        props[child.tag] = child
            out.append((unquote(urlsplit(href).path), props))
        return out

    @staticmethod
    def _entry(name, props):
        rt = props.get(_DAV + 'resourcetype')
        is_dir = rt is not None and rt.find(_DAV + 'collection') is not None
        mtime = size = etag = None
        lm = props.get(_DAV + 'getlastmodified')
        if lm is not None and (lm.text or '').strip() and not is_dir:
            try: mtime = parsedate_to_datetime(lm.text.strip()).timestamp()
            except (TypeError, ValueError): pass
        cl = props.get(_DAV + 'getcontentlength')
        if cl is not None and (cl.text or '').strip().isdigit():
            size = int(cl.text.strip())
        et = props.get(_DAV + 'getetag')
        if et is not None and (et.text or '').strip():
            etag = et.text.strip().strip('"')
        return Entry(name, is_dir, mtime, size, etag)

    def _own_path(self, path):
        return unquote(urlsplit(self._url(path)).path).rstrip('/')

    def scandir(self, path):
        me = self._own_path(path)
        out = []
        for href_path, props in self._propfind(path, 1):
            hp = href_path.rstrip('/')
            if hp == me:
                continue   # the directory itself
            out.append(self._entry(posixpath.basename(hp), props))
        return out

    def stat(self, path):
        res = self._propfind(path, 0)
        if not res:
            raise FileNotFoundError(path)
        return self._entry(self.basename(path), res[0][1])

    def open(self, path, mode='rb'):
        if mode == 'rb':
            return io.BufferedReader(_RangeReader(self, path), buffer_size=256 * 1024)
        if mode == 'wb':
            return _UploadOnClose(self, path, b'')
        if mode == 'rb+':
            data = self._req('GET', path, ok=(200,)).content
            return _UploadOnClose(self, path, data)
        raise ValueError(f'unsupported mode {mode!r} for WebDAV')

    def remove(self, path):
        self._req('DELETE', path, ok=(200, 204))

    def upload(self, path, data):
        """Upload via a temporary name + MOVE so readers never see a half-written file."""
        tmp = self.join(self.dirname(path), f'.artmgr-tmp-{uuid.uuid4().hex[:8]}-{self.basename(path)}')
        self._req('PUT', tmp, data=data, headers={'Content-Type': 'application/octet-stream'})
        try:
            self._req('MOVE', tmp, headers={'Destination': self._url(path), 'Overwrite': 'T'})
        except Exception:
            try: self._req('DELETE', tmp, ok=(200, 204, 404))
            except Exception: pass
            raise
        size = self.stat(path).size
        if size is not None and size != len(data):
            raise WebDAVError(f'upload of {path} could not be verified: '
                              f'server has {size} bytes, expected {len(data)}')

    def error_message(self, e, path):
        low = str(e).lower()
        if isinstance(e, PermissionError):
            return f"WebDAV access denied for {path!r} — check the username/password in Settings."
        if isinstance(e, FileNotFoundError):
            return f"Path not found on the WebDAV server: {path!r}"
        if isinstance(e, requests.exceptions.ConnectionError) or 'timed out' in low:
            return f"Can't reach the WebDAV server for {path!r} — check the URL and that the server is online."
        return f"WebDAV error accessing {path!r}: {e}"


class _RangeReader(io.RawIOBase):
    """Seekable read-only view of a remote file, fetched in HTTP Range requests on demand."""

    def __init__(self, store, path):
        self._store, self._path, self._pos = store, path, 0
        self._size = store.stat(path).size
        self._whole = None   # set if the server ignores Range

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self._pos

    def seek(self, off, whence=0):
        if whence == 0: self._pos = off
        elif whence == 1: self._pos += off
        else: self._pos = (self._size or 0) + off
        return self._pos

    def readinto(self, b):
        n = len(b)
        if n == 0 or (self._size is not None and self._pos >= self._size):
            return 0
        if self._whole is None:
            end = self._pos + n - 1
            r = self._store._req('GET', self._path, ok=(200, 206),
                                 headers={'Range': f'bytes={self._pos}-{end}'})
            if r.status_code == 200:          # no Range support: keep the whole body
                self._whole = r.content
                self._size = len(self._whole)
            else:
                chunk = r.content
                b[:len(chunk)] = chunk
                self._pos += len(chunk)
                return len(chunk)
        chunk = self._whole[self._pos:self._pos + n]
        b[:len(chunk)] = chunk
        self._pos += len(chunk)
        return len(chunk)


class _UploadOnClose(io.BytesIO):
    """In-memory file; on close, uploads the contents if they changed (or always, for 'wb')."""

    def __init__(self, store, path, initial):
        super().__init__(initial)
        self._store, self._path = store, path
        self._orig = initial
        self._always = not initial

    def close(self):
        if not self.closed:
            data = self.getvalue()
            try:
                if self._always or data != self._orig:
                    self._store.upload(self._path, data)
            finally:
                super().close()

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:          # caller failed: don't upload a half-edited file
            super().close()
            return False
        self.close()
        return False


# ── Dispatch ──────────────────────────────────────────────────────────────────

_local = LocalStorage()
_backends = {'smb': SMBStorage(), 'webdav': WebDAVStorage()}
_schemes = {'smb': 'smb', 'webdav': 'webdav', 'webdavs': 'webdav', 'http': 'webdav', 'https': 'webdav'}

def register(scheme, backend):
    """Add a backend for a URL scheme (used by tests for a counting/faulty fake)."""
    _backends[scheme] = backend
    _schemes[scheme] = scheme

def backend_for(path):
    m = re.match(r'^([A-Za-z][A-Za-z0-9+.-]*)://', path or '')
    if m and m.group(1).lower() in _schemes:
        return _backends[_schemes[m.group(1).lower()]]
    return _local

def parallelism(path):
    return backend_for(path).parallel

def kind(path):
    """'local' | 'smb' | 'webdav' (or a registered test scheme)."""
    return backend_for(path).name

def reset_all():
    for b in _backends.values():
        b.reset()

def scandir(path):          return backend_for(path).scandir(path)
def stat(path):             return backend_for(path).stat(path)
def open_file(path, mode='rb'): return backend_for(path).open(path, mode)
def remove(path):           return backend_for(path).remove(path)
def join(base, *parts):     return backend_for(base).join(base, *parts)
def basename(path):         return backend_for(path).basename(path)
def dirname(path):          return backend_for(path).dirname(path)
def fingerprint(path):      return backend_for(path).fingerprint(path)
def error_message(e, path): return backend_for(path).error_message(e, path)

def is_not_found(e):
    return isinstance(e, FileNotFoundError) or getattr(e, 'errno', None) == errno.ENOENT

def is_permanent(e):
    """Errors that retrying won't fix (missing path, not a directory, access denied)."""
    if isinstance(e, (FileNotFoundError, NotADirectoryError, PermissionError)):
        return True
    return getattr(e, 'errno', None) in (errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM)

def isdir(path):
    try: return stat(path).is_dir
    except Exception as e:
        if is_not_found(e): return False
        raise

def isfile(path):
    try: return not stat(path).is_dir
    except Exception as e:
        if is_not_found(e): return False
        raise

def read_bytes(path):
    with open_file(path, 'rb') as f:
        return f.read()

def write_bytes(path, data):
    with open_file(path, 'wb') as f:
        f.write(data)
