"""Settings: defaults, config.json load/save/migration, env overrides, ignore list."""
import base64, fnmatch, hashlib, hmac, json, os, re, sys
from urllib.parse import urlsplit, unquote

def _default_data_dir():
    """
    Where config.json / library_cache.json live:
      1. ARTMGR_DATA_DIR if set (Docker image, UI tests);
      2. a source checkout: the repo root, next to app.py (as before);
      3. a pip install: the per-user config dir (~/.config/art-manager,
         %APPDATA%\\art-manager on Windows).
    """
    if os.environ.get('ARTMGR_DATA_DIR'):
        return os.environ['ARTMGR_DATA_DIR']
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not getattr(sys, 'frozen', False) and os.path.exists(os.path.join(repo, 'app.py')):
        return repo
    if sys.platform == 'darwin':
        d = os.path.expanduser('~/Library/Application Support/Art Manager')
        os.makedirs(d, exist_ok=True)
        return d
    base = os.environ.get('APPDATA') if os.name == 'nt' else \
        (os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'))
    d = os.path.join(base or os.path.expanduser('~'), 'art-manager')
    os.makedirs(d, exist_ok=True)
    return d

DATA_DIR = _default_data_dir()
CFG_FILE = os.path.join(DATA_DIR, 'config.json')
CACHE_FILE = os.path.join(DATA_DIR, 'library_cache.json')

DEFAULTS = {
    'library_root': '',
    # Credentials for the library's storage backend (SMB or WebDAV). Never put
    # them in the URL; _migrate() moves them out if an old config did.
    'storage_username': '', 'storage_password': '', 'smb_port': 445,
    'fanart_key': '', 'discogs_token': '', 'web_search': True,
    'ignored_folders': None,   # None = DEFAULT_IGNORED; stored once edited
    # How the app is reached (take effect on the next start; CLI flags override)
    'allow_network': False,    # False = this computer only (127.0.0.1)
    'port': 5000,
    'app_password_hash': '',   # PBKDF2 hash of the optional app password; never the password itself
}
cfg = dict(DEFAULTS)   # the one live settings dict; mutate it, never rebind it

# Folders skipped at every level (artist, album, disc). Case-insensitive
# shell-style patterns. Only defaults: they show up in Settings and can be
# removed. '.[!.]*' rather than '.*' so names like "...And You Will Know Us by
# the Trail of Dead" are not hidden, while .AppleDouble etc. are.
DEFAULT_IGNORED = ['.[!.]*', '@eaDir', '#recycle', '@Recycle', '$RECYCLE.BIN',
                   'System Volume Information']

# Never sent to the browser: GET /api/config replaces them with has_<key>
# flags; POST only overwrites one when a non-empty value is sent.
SECRET_KEYS = ('storage_password', 'fanart_key', 'discogs_token', 'app_password_hash')

# Environment variables override config.json and are never written to it.
ENV_OVERRIDES = {
    'ARTMGR_LIBRARY_ROOT':      'library_root',
    'ARTMGR_STORAGE_USERNAME':  'storage_username',
    'ARTMGR_STORAGE_PASSWORD':  'storage_password',
    'ARTMGR_SMB_USERNAME':      'storage_username',   # older names, still accepted
    'ARTMGR_SMB_PASSWORD':      'storage_password',
    'ARTMGR_FANART_KEY':        'fanart_key',
    'ARTMGR_DISCOGS_TOKEN':     'discogs_token',
}
_env_cfg = {}   # values that came from the environment


def _migrate(c):
    """
    Upgrade older config shapes in place. Returns True if anything changed.
      - smb_username / smb_password  -> storage_username / storage_password
      - credentials pasted into the URL ('smb://user:pass@host/share') -> fields
      - a bare UNC path plus 'use_smb' flag -> 'smb://host/share/path'
      - settings that no longer exist are dropped
    """
    changed = False
    for old, new in (('smb_username', 'storage_username'), ('smb_password', 'storage_password')):
        if old in c:
            val = c.pop(old)
            if val and not c.get(new):
                c[new] = val
            changed = True

    root = c.get('library_root', '') or ''
    scheme = root.split('://', 1)[0].lower() if '://' in root else ''
    if scheme in ('smb', 'webdav', 'webdavs', 'http', 'https'):
        u = urlsplit(root)
        if u.username or u.password:
            netloc = u.hostname or ''
            if u.port and scheme != 'smb':
                netloc += f':{u.port}'
            c['library_root'] = f'{scheme}://{netloc}{u.path}'
            if u.username: c['storage_username'] = unquote(u.username)
            if u.password: c['storage_password'] = unquote(u.password)
            if u.port and scheme == 'smb': c['smb_port'] = u.port
            changed = True
    if c.pop('use_smb', False) and root.startswith('\\\\'):
        c['library_root'] = 'smb://' + root.lstrip('\\').replace('\\', '/')
        changed = True

    for k in [k for k in c if k not in DEFAULTS]:
        c.pop(k)
        changed = True
    return changed


def load_cfg():
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    if _migrate(cfg):
        save_cfg()
    for env, key in ENV_OVERRIDES.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
            _env_cfg[key] = val


def save_cfg():
    out = {k: v for k, v in cfg.items() if not (k in _env_cfg and _env_cfg[k] == v)}
    # Created with 0600 from the start, so secrets are never briefly world-readable.
    fd = os.open(CFG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(out, f, indent=2)
    try: os.chmod(CFG_FILE, 0o600)   # tighten a file created by an older version
    except OSError: pass


def public_cfg():
    """The config as the browser may see it: secrets replaced by has_<key> flags."""
    out = {k: v for k, v in cfg.items() if k not in SECRET_KEYS}
    out['ignored_folders'] = ignored_patterns()
    out['default_ignored_folders'] = DEFAULT_IGNORED
    for k in SECRET_KEYS:
        out['has_' + k] = bool(cfg.get(k))
    return out


# ── App password ──────────────────────────────────────────────────────────────

_PBKDF2_ITERATIONS = 200_000

def hash_password(pw):
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt, _PBKDF2_ITERATIONS)
    return 'pbkdf2_sha256${}${}${}'.format(_PBKDF2_ITERATIONS, base64.b64encode(salt).decode(),
                                            base64.b64encode(dk).decode())

def verify_password(pw, stored):
    try:
        algo, iters, salt, dk = stored.split('$')
        assert algo == 'pbkdf2_sha256'
        got = hashlib.pbkdf2_hmac('sha256', pw.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except Exception:
        return False


# ── Ignore list ───────────────────────────────────────────────────────────────

def ignored_patterns():
    pats = cfg.get('ignored_folders')
    return DEFAULT_IGNORED if pats is None else pats

def is_ignored(name):
    low = (name or '').lower()
    return any(fnmatch.fnmatchcase(low, p.lower()) for p in ignored_patterns())

def parse_patterns(v):
    """Accept a list or a newline/comma separated string; drop blanks and dupes."""
    items = v if isinstance(v, list) else re.split(r'[\n,]', str(v or ''))
    out = []
    for it in items:
        it = str(it).strip()
        if it and it not in out:
            out.append(it)
    return out
