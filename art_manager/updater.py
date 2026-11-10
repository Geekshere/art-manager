"""
Self-update for the standalone app (GitHub Releases).

- check():    ask GitHub for the latest release (at most every few hours in
              the background; "Check now" in Settings forces it).
- download(): fetch this platform's file, verify size + SHA-256 (GitHub
              publishes digests), make sure it actually runs (`--version`),
              and keep it ready ("staged") in <data dir>/update/.
- apply():    swap the program file and restart. Linux/macOS replace the file
              and exec the new one; Windows renames the running .exe out of
              the way (allowed while it runs), moves the new one in, starts
              it and exits. Settings live in the data dir, so they are kept.

A staged update is also installed automatically at the next start. Other
kinds of installs (pip/uv, source checkout, Docker) get the right command to
run instead.
"""
import hashlib, json, logging, os, platform, shutil, subprocess, sys, threading, time
import ssl, urllib.request, zipfile

from . import __version__, config

log = logging.getLogger('art_manager.update')

REPO = 'Geekshere/art-manager'
DOWNLOAD_PREFIXES = (f'https://github.com/{REPO}/releases/download/',)
CHECK_EVERY = 6 * 3600

_lock = threading.Lock()
state = {
    'status': 'idle',          # idle | checking | downloading | ready | installing | error
    'latest': None, 'notes': '', 'page': '', 'checked_at': None,
    'progress': 0, 'error': None, 'staged': None,
}


def api_url():
    return os.environ.get('ARTMGR_UPDATE_URL') or f'https://api.github.com/repos/{REPO}/releases/latest'

def parse_version(v):
    """'v0.10.2' -> (0, 10, 2); anything odd -> (0,)."""
    try:
        return tuple(int(x) for x in str(v).lstrip('vV').split('-')[0].split('.'))
    except ValueError:
        return (0,)

def newer(a, b=__version__):
    return parse_version(a) > parse_version(b)


def install_kind():
    """'app' (standalone download), 'docker', 'source' (git checkout) or 'pip' (pip/uv/pipx)."""
    if getattr(sys, 'frozen', False):
        return 'app'
    if os.path.exists('/.dockerenv') or os.environ.get('ARTMGR_IN_DOCKER'):
        return 'docker'
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(repo, 'app.py')):
        return 'source'
    return 'pip'

def how_to_update(kind=None):
    kind = kind or install_kind()
    return {
        'app': '',
        'source': 'In the art-manager folder run:  git pull  — then start the app again.',
        'pip': 'Run:  uv tool upgrade art-manager   (or: pipx upgrade art-manager) — then start the app again.',
        'docker': 'Rebuild or pull the image and recreate the container (your settings are in the /data volume).',
    }[kind]

def asset_name():
    """The release file for this computer, or None if there is no standalone build for it."""
    machine = platform.machine().lower()
    if sys.platform == 'win32':
        return 'art-manager-windows-x64.exe'
    if sys.platform == 'darwin':
        return 'art-manager-macos-arm64.zip' if machine in ('arm64', 'aarch64') else None
    if sys.platform.startswith('linux') and machine in ('x86_64', 'amd64'):
        return 'art-manager-linux-x64'
    return None

def _update_dir():
    d = os.path.join(config.DATA_DIR, 'update')
    os.makedirs(d, exist_ok=True)
    return d

def _program():
    return os.path.abspath(sys.executable)

def can_replace_program():
    """The standalone app can only update itself if its folder is writable."""
    if install_kind() != 'app':
        return False
    return os.access(os.path.dirname(_program()), os.W_OK)


def public_state():
    with _lock:
        out = dict(state)
    kind = install_kind()
    # what is on disk is the truth (a download may come from an earlier run)
    staged = staged_update() if kind == 'app' else None
    out['staged'] = staged
    if staged and out['status'] in ('idle', 'ready'):
        out['status'] = 'ready'
    out.update(current=__version__, kind=kind, how_to_update=how_to_update(kind),
               available=bool(out['latest'] and newer(out['latest'])),
               can_self_update=kind == 'app' and asset_name() is not None,
               writable=can_replace_program(), program=_program() if kind == 'app' else None,
               auto_check=config.cfg.get('update_check', True),
               auto_install=config.cfg.get('update_auto_install', True))
    out.pop('_asset', None)
    return out


# ── check ─────────────────────────────────────────────────────────────────────

def _ssl_context():
    """
    Certificates from certifi (shipped with the app): the standalone macOS app
    can't see a certificate store, so the default context fails there with
    CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

def _open(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())

def _get_json(url):
    req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                               'User-Agent': f'art-manager/{__version__}'})
    with _open(req, 15) as r:
        return json.load(r)

def check():
    """Ask GitHub for the latest release. Never raises; the result is in state."""
    with _lock:
        if state['status'] in ('downloading', 'installing'):
            return public_state()
        state.update(status='checking', error=None)
    try:
        rel = _get_json(api_url())
        tag = rel.get('tag_name', '')
        name = asset_name()
        asset = next((a for a in rel.get('assets', []) if a.get('name') == name), None)
        with _lock:
            state.update(latest=tag.lstrip('vV'), notes=(rel.get('body') or '')[:4000],
                         page=rel.get('html_url', ''), checked_at=time.time(), _asset=asset,
                         status='ready' if state.get('staged') and _staged_version() == tag.lstrip('vV') else 'idle')
        log.info('update check: latest %s (running %s)', tag, __version__)
    except Exception as e:
        with _lock:
            state.update(status='error', error=f"Couldn't check for updates: {e}", checked_at=time.time())
        log.info('update check failed: %r', e)
    return public_state()


# ── download ──────────────────────────────────────────────────────────────────

def _staged_path(version):
    ext = '.exe' if sys.platform == 'win32' else ''
    return os.path.join(_update_dir(), f'art-manager-{version}{ext}')

def _staged_version():
    for name in os.listdir(_update_dir()):
        if name.startswith('art-manager-') and not name.endswith(('.part', '.zip')):
            return name[len('art-manager-'):].replace('.exe', '')
    return None

def _verify_runs(path, version):
    """The downloaded program must start and report the expected version."""
    try:
        out = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=120).stdout
    except Exception as e:
        raise RuntimeError(f'the downloaded program does not start ({e})')
    if version not in out:
        raise RuntimeError(f'the downloaded program reports {out.strip()!r}, expected {version}')

def download():
    """Download, verify and stage the latest release for this platform. Returns the staged path."""
    with _lock:
        asset, version = state.get('_asset'), state.get('latest')
        if state['status'] in ('downloading', 'installing'):
            raise RuntimeError('an update is already in progress')
    if not version or not newer(version):
        raise RuntimeError('no newer version to install')
    if not asset:
        raise RuntimeError(f'no download for this computer in release {version}')
    url = asset.get('browser_download_url', '')
    if not (url.startswith(DOWNLOAD_PREFIXES) or os.environ.get('ARTMGR_UPDATE_URL')):
        raise RuntimeError(f'refusing to download from an unexpected address: {url}')
    final = _staged_path(version)
    if os.path.exists(final):
        with _lock:
            state.update(status='ready', staged=final, progress=100)
        return final

    with _lock:
        state.update(status='downloading', progress=0, error=None)
    part = os.path.join(_update_dir(), asset['name'] + '.part')
    try:
        expected_size = int(asset.get('size') or 0)
        sha = hashlib.sha256()
        req = urllib.request.Request(url, headers={'User-Agent': f'art-manager/{__version__}'})
        with _open(req, 60) as r, open(part, 'wb') as f:
            total = expected_size or int(r.headers.get('Content-Length') or 0)
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk); sha.update(chunk); done += len(chunk)
                if total:
                    with _lock:
                        state['progress'] = min(99, int(done * 100 / total))
        if expected_size and done != expected_size:
            raise RuntimeError(f'download incomplete ({done} of {expected_size} bytes)')
        digest = (asset.get('digest') or '').lower()
        if digest.startswith('sha256:') and digest[7:] != sha.hexdigest():
            raise RuntimeError('download is corrupted (checksum mismatch)')

        staged_tmp = final + '.part'
        if asset['name'].endswith('.zip'):
            with zipfile.ZipFile(part) as z:
                member = next(n for n in z.namelist() if not n.endswith('/') and 'art-manager' in os.path.basename(n))
                with z.open(member) as src, open(staged_tmp, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
            os.remove(part)
        else:
            os.replace(part, staged_tmp)
        os.chmod(staged_tmp, 0o755)
        _verify_runs(staged_tmp, version)
        os.replace(staged_tmp, final)
        with _lock:
            state.update(status='ready', staged=final, progress=100)
        log.info('update %s downloaded and verified: %s', version, final)
        return final
    except Exception as e:
        for p in (part, final + '.part'):
            try: os.remove(p)
            except OSError: pass
        with _lock:
            state.update(status='error', error=f'Update download failed: {e}', progress=0)
        log.warning('update download failed: %r', e)
        raise


# ── install ───────────────────────────────────────────────────────────────────

def apply(staged, restart_args):
    """
    Replace the running program with `staged` and start it. Does not return on
    success (this process is replaced or exits).
    """
    prog = _program()
    with _lock:
        state.update(status='installing')
    log.info('installing update from %s over %s', staged, prog)
    # The standalone app unpacks itself to a temp folder and marks it in the
    # environment; a new copy inheriting that would reuse the OLD version's
    # folder (being deleted as we exit). PyInstaller's switch for "start fresh":
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    if sys.platform == 'win32':
        old = prog + f'.old-{int(time.time())}'
        os.replace(prog, old)                     # a running .exe can be renamed, not overwritten
        log.info('moved the running program aside: %s', old)
        try:
            shutil.copy2(staged, prog)
        except Exception:
            os.replace(old, prog)                 # put the working one back
            raise
        log.info('new program in place, starting: %s %s', prog, restart_args)
        flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
        p = subprocess.Popen([prog] + restart_args, creationflags=flags, close_fds=True, env=env)
        log.info('started the new version (pid %s); this one exits', p.pid)
        for h in logging.getLogger('art_manager').handlers:
            try: h.flush()
            except Exception: pass
        os._exit(0)
    else:
        tmp = prog + '.new'
        shutil.copy2(staged, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, prog)                     # atomic; the running process keeps its old copy
        # The web server's listening socket is inheritable (Werkzeug sets that
        # for its reloader); carried through exec it would keep the port busy
        # with nobody answering. Close everything but stdin/out/err first.
        os.closerange(3, 65536)
        os.execve(prog, [prog] + restart_args, env)


def cleanup():
    """Remove leftovers from earlier updates (old .exe files, staged files no longer needed)."""
    if install_kind() != 'app':
        return
    prog_dir = os.path.dirname(_program())
    base = os.path.basename(_program())
    for name in os.listdir(prog_dir):
        if name.startswith(base + '.old-') or name == base + '.new':
            try: os.remove(os.path.join(prog_dir, name))
            except OSError: pass            # still running on Windows: removed next time
    try:
        for name in os.listdir(_update_dir()):
            v = name[len('art-manager-'):].replace('.exe', '').replace('.part', '')
            if name.endswith('.part') or not newer(v):
                os.remove(os.path.join(_update_dir(), name))
    except OSError:
        pass


def staged_update():
    """Path of a downloaded, verified update newer than this version, or None."""
    try:
        v = _staged_version()
    except OSError:
        return None
    if v and newer(v):
        p = _staged_path(v)
        if os.path.exists(p):
            return p
    return None


def background(stop_event=None):
    """Check every few hours; with auto-install on, download so the next start installs it."""
    time.sleep(5)
    while True:
        try:
            if config.cfg.get('update_check', True):
                st = check()
                if (st['available'] and st['can_self_update'] and st['writable']
                        and config.cfg.get('update_auto_install', True) and not staged_update()):
                    try:
                        download()
                    except Exception:
                        pass
        except Exception as e:
            log.info('background update check failed: %r', e)
        for _ in range(CHECK_EVERY):
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(1)
