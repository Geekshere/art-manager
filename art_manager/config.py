"""Settings: defaults, config.json load/save/migration, env overrides, ignore list."""
import base64, fnmatch, hashlib, hmac, json, os, re, sys
from urllib.parse import urlsplit, unquote

def _default_data_dir():
    """
    Where config.json / library_cache.json live (all of these persist across
    restarts and app updates):
      1. ARTMGR_DATA_DIR if set (Docker image, UI tests);
      2. standalone app in "portable mode": a folder named art-manager-data
         next to the program file is used if it exists (settings travel with
         the app, e.g. on a USB stick);
      3. a source checkout: the repo root, next to app.py;
      4. otherwise the per-user settings folder: %APPDATA%\\art-manager
         (Windows), ~/Library/Application Support/Art Manager (macOS),
         ~/.config/art-manager (Linux).
    """
    if os.environ.get('ARTMGR_DATA_DIR'):
        d = os.environ['ARTMGR_DATA_DIR']
        os.makedirs(d, exist_ok=True)
        return d
    if getattr(sys, 'frozen', False):
        portable = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), 'art-manager-data')
        if os.path.isdir(portable) and os.access(portable, os.W_OK):
            return portable
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
    'fanart_key': '', 'discogs_token': '', 'redacted_key': '', 'web_search': True,
    'ignored_folders': None,   # None = DEFAULT_IGNORED; stored once edited
    # How the app is reached (take effect on the next start; CLI flags override)
    'allow_network': False,    # False = this computer only (127.0.0.1)
    'port': 5000,
    'app_password_hash': '',   # PBKDF2 hash of the optional app password; never the password itself
    # What plays the library; decides the file names below (see LIBRARY_PRESETS).
    'library_type': None,        # None = not chosen yet (the setup screen asks)
    'cover_name': 'cover.jpg',   # album cover file, in the album folder (and disc folders)
    'artist_image_name': 'artist.jpg',
    'disc_covers': True,         # multi-disc albums also need the cover in every disc folder
    'embed_art': True,           # also embed the cover into every track
    'auto_tidy': True,           # after a scan/check: cover.png + cover.jpg -> one real cover.jpg, old copy deleted
    'update_check': True,        # look for new versions on GitHub every few hours
    'update_auto_install': True, # standalone app: download them and install at the next start
    'settings_version': 3,     # bumped when a migration in _migrate() must run once
}
cfg = dict(DEFAULTS)   # the one live settings dict; mutate it, never rebind it

# Folders skipped at every level (artist, album, disc). Case-insensitive
# shell-style patterns. Only defaults: they show up in Settings and can be
# removed. '.[!.]*' rather than '.*' so names like "...And You Will Know Us by
# the Trail of Dead" are not hidden, while .AppleDouble etc. are.
DEFAULT_IGNORED = ['.[!.]*', '@eaDir', '#recycle', '@Recycle', '$RECYCLE.BIN',
                   'System Volume Information', '!Playlists']

# Never sent to the browser: GET /api/config replaces them with has_<key>
# flags; POST only overwrites one when a non-empty value is sent.
SECRET_KEYS = ('storage_password', 'fanart_key', 'discogs_token', 'redacted_key', 'app_password_hash')

# Environment variables override config.json and are never written to it.
ENV_OVERRIDES = {
    'ARTMGR_LIBRARY_ROOT':      'library_root',
    'ARTMGR_STORAGE_USERNAME':  'storage_username',
    'ARTMGR_STORAGE_PASSWORD':  'storage_password',
    'ARTMGR_SMB_USERNAME':      'storage_username',   # older names, still accepted
    'ARTMGR_SMB_PASSWORD':      'storage_password',
    'ARTMGR_FANART_KEY':        'fanart_key',
    'ARTMGR_DISCOGS_TOKEN':     'discogs_token',
    'ARTMGR_REDACTED_KEY':      'redacted_key',
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

    # v2: '!Playlists' joined the default ignore list; add it to lists saved before.
    if c.get('settings_version', 1) < 2:
        pats = c.get('ignored_folders')
        if isinstance(pats, list) and not any(p.lower() == '!playlists' for p in pats):
            pats.append('!Playlists')
        c['settings_version'] = 2
        changed = True

    # v3: library types. Existing setups keep exactly what they had (Navidrome naming).
    if c.get('settings_version', 1) < 3:
        if c.get('library_root') and not c.get('library_type'):
            c['library_type'] = 'navidrome'
        c['settings_version'] = 3
        changed = True

    for k in [k for k in c if k not in DEFAULTS]:
        c.pop(k)
        changed = True
    return changed


def load_cfg():
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE) as f:
                saved = json.load(f)
            saved.setdefault('settings_version', 1)   # files from before versioning
            cfg.update(saved)
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
    out['library_presets'] = LIBRARY_PRESETS
    out['naming_key'] = naming_key()
    for k in SECRET_KEYS:
        out['has_' + k] = bool(cfg.get(k))
    return out


# ── Library types / naming ────────────────────────────────────────────────────

LIBRARY_PRESETS = {
    'navidrome': {
        'label': 'Navidrome', 'cover_name': 'cover.jpg', 'artist_image_name': 'artist.jpg',
        'disc_covers': True, 'embed_art': True,
        'note': 'Navidrome reads cover.jpg (album folder), artist.jpg (artist folder) and embedded art. '
                'A cover in each disc folder lets it show per-disc art.'},
    'plex': {
        'label': 'Plex / Plexamp', 'cover_name': 'cover.jpg', 'artist_image_name': 'artist.jpg',
        'disc_covers': False, 'embed_art': True,
        'note': 'Plex reads cover.jpg in the album folder and artist.jpg in the artist folder when the '
                'library\'s "Use local assets" option is on; it also reads embedded art.'},
    'jellyfin': {
        'label': 'Jellyfin', 'cover_name': 'folder.jpg', 'artist_image_name': 'folder.jpg',
        'disc_covers': False, 'embed_art': True,
        'note': 'Jellyfin reads folder.jpg in album folders and in artist folders, and embedded art.'},
    'custom': {
        'label': 'Other / my own naming', 'note': 'Choose the file names yourself.'},
}
NAMING_KEYS = ('cover_name', 'artist_image_name', 'disc_covers', 'embed_art')
IMAGE_NAME = re.compile(r'^[^/\\:*?"<>|]+\.(?:jpe?g|png)$', re.I)

def apply_library_type(kind, custom=None):
    """Set library_type and its naming; for 'custom', take the names from `custom`."""
    if kind not in LIBRARY_PRESETS:
        raise ValueError(f'unknown library type {kind!r}')
    cfg['library_type'] = kind
    if kind != 'custom':
        for k in NAMING_KEYS:
            cfg[k] = LIBRARY_PRESETS[kind][k]
    if custom:
        set_naming(custom)

def set_naming(d):
    for k in ('cover_name', 'artist_image_name'):
        if k in d:
            v = str(d[k] or '').strip()
            if not IMAGE_NAME.match(v):
                raise ValueError(f'{v!r} is not a usable image file name (use e.g. cover.jpg or folder.png)')
            cfg[k] = v
    for k in ('disc_covers', 'embed_art'):
        if k in d:
            cfg[k] = bool(d[k])

def naming_key():
    """Changes when anything that decides 'has a cover' changes (cached scans are then redone)."""
    return '|'.join(str(cfg.get(k)) for k in NAMING_KEYS)


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
