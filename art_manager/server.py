"""Flask app: HTTP API, embed jobs (SSE), access control and the CLI entry point."""
import argparse, concurrent.futures, hashlib, hmac, json, logging, os, posixpath, re, socket, string
import subprocess, sys, threading, time, traceback, urllib.request, uuid, webbrowser

from flask import Flask, Response, jsonify, render_template, request

from . import __version__, check, config, formats, storage, tags, updater
from .artwork import (collect_album_flacs, decode_uploaded_image, dl_image, embed_into,
                      find_album_embedded, fix_album_covers, sniff_image, tidy_album, tidy_artist,
                      to_data_url, to_jpeg, track_pictures,
                      write_artist_img, write_covers)
from .config import cfg, is_ignored, load_cfg, parse_patterns, public_cfg, save_cfg
from .scan import SCHEMA_VERSION, apply_ignore, client_view, is_track, list_dir, scan_library
from .search import run_providers

app = Flask(__name__)
log = logging.getLogger('art_manager')

# ── Library cache (in memory + on disk so restarts don't lose it) ────────────

_cached_lib = None

def _save_lib_cache(data):
    try:
        with open(config.CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception:
        pass

def _load_lib_cache():
    try:
        with open(config.CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def _library():
    global _cached_lib
    if not _cached_lib:
        _cached_lib = _load_lib_cache()
    return _cached_lib

# ── Path safety ───────────────────────────────────────────────────────────────

def _norm(p):
    if storage.kind(p) == 'local' and not p.lower().startswith('file://'):
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))
    scheme, _, rest = p.partition('://')
    return scheme.lower() + '://' + posixpath.normpath('/' + rest).lstrip('/')

def _in_library(path):
    """True if path is inside the configured/scanned library root (no '..' escapes)."""
    if not path:
        return False
    roots = {cfg.get('library_root') or ''}
    lib = _library()
    if lib and lib.get('root'):
        roots.add(lib['root'])
    if '/../' in path.replace('\\', '/') + '/':
        return False
    p = _norm(path)
    for r in filter(None, roots):
        r = _norm(r).rstrip('/\\')
        if p == r or p.startswith(r + os.sep) or p.startswith(r + '/'):
            return True
    return False

def _check_paths(*paths):
    bad = [p for p in paths if not _in_library(p)]
    if bad:
        return jsonify({'error': f'Path is outside the library: {bad[0]!r}'}), 403
    return None

# ── Access control ────────────────────────────────────────────────────────────

# Set at startup. 'password' is a plain password from --password/ARTMGR_PASSWORD
# (overrides the saved one); otherwise cfg['app_password_hash'] is used.
_auth = {'password': None, 'loopback_only': False}
_running = {'host': None, 'port': None}
_verified = set()   # sha256 of passwords already checked against the slow PBKDF2 hash

def _password_ok(pw):
    if _auth['password']:
        return hmac.compare_digest(pw.encode(), _auth['password'].encode())
    stored = cfg.get('app_password_hash')
    if not stored:
        return True
    key = hashlib.sha256((stored + '\0' + pw).encode()).hexdigest()
    if key in _verified:
        return True
    if config.verify_password(pw, stored):
        _verified.add(key)
        return True
    return False

def _password_required():
    return bool(_auth['password'] or cfg.get('app_password_hash'))
_LOOPBACK_HOSTNAMES = {'localhost', '127.0.0.1', '[::1]'}

def _is_loopback(host):
    return host in ('127.0.0.1', 'localhost', '::1') or host.startswith('127.')

@app.before_request
def _require_password():
    # When bound to loopback, refuse requests addressed to any other hostname.
    # That blocks DNS rebinding: a page on evil.example that re-points its own
    # name at 127.0.0.1 still sends "Host: evil.example".
    if _auth['loopback_only']:
        host = request.host or ''
        hostname = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
        if hostname.lower() not in _LOOPBACK_HOSTNAMES:
            return Response('Forbidden host', 403)
    if not _password_required() or request.path == '/api/ping':
        return None
    a = request.authorization
    if a and a.password is not None and _password_ok(a.password):
        return None
    return Response('Password required', 401, {'WWW-Authenticate': 'Basic realm="Art Manager"'})

@app.after_request
def _compress(resp):
    # The library listing can be several MB for a big collection; gzip makes
    # it ~10x smaller (matters on a phone over Wi-Fi). Only plain JSON bodies:
    # not images (already compressed) or the embed progress stream.
    if (resp.direct_passthrough or resp.status_code != 200 or resp.mimetype != 'application/json'
            or 'gzip' not in request.headers.get('Accept-Encoding', '')
            or 'Content-Encoding' in resp.headers):
        return resp
    data = resp.get_data()
    if len(data) < 16 * 1024:
        return resp
    import gzip
    resp.set_data(gzip.compress(data, compresslevel=5))
    resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Content-Length'] = str(len(resp.get_data()))
    resp.vary.add('Accept-Encoding')
    return resp

@app.after_request
def _no_store(resp):
    # Images change in place (a new cover.jpg has the same path) and search
    # results must never be replayed from cache for a different step.
    if request.path.startswith(('/api/image/', '/api/search/', '/api/track-art')):
        resp.headers['Cache-Control'] = 'no-store'
    return resp

# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', version=__version__)

@app.route('/<path:client_path>')
def spa_route(client_path):
    # Client routes are /<artist>/<album>/ with each name encodeURIComponent'd.
    # The WSGI server decodes %2F to '/', so "A/B Testing?" arrives as extra
    # segments — match any depth; the client resolves window.location.pathname.
    if client_path.startswith('api/'):
        return jsonify({'error': 'Not found'}), 404
    return render_template('index.html', version=__version__)

@app.route('/api/ping')
def api_ping():
    """Lets a second launch find this one (and open the browser instead of starting again)."""
    return jsonify({'app': 'art-manager', 'version': __version__})

@app.route('/api/update', methods=['GET'])
def api_update_status():
    return jsonify(updater.public_state())

@app.route('/api/update/check', methods=['POST'])
def api_update_check():
    if not request.is_json:
        return jsonify({'error': 'JSON required'}), 400
    return jsonify(updater.check())

@app.route('/api/update/install', methods=['POST'])
def api_update_install():
    """Download (if needed), verify, swap the program and restart. The page reloads itself."""
    if not request.is_json:
        return jsonify({'error': 'JSON required'}), 400
    st = updater.public_state()
    if not st['can_self_update']:
        return jsonify({'error': st['how_to_update'] or 'No standalone app for this computer'}), 400
    if not st['writable']:
        return jsonify({'error': f"Can't replace {st['program']}: its folder is read-only. Move the app to "
                                 f"a folder you can write to (e.g. your home folder) or download the new "
                                 f"version manually."}), 400
    if not st['available'] and not updater.staged_update():
        return jsonify({'error': 'Already up to date'}), 400

    def run():
        try:
            staged = updater.staged_update() or updater.download()
            time.sleep(0.8)   # let the browser receive the "restarting" status
            updater.apply(staged, _restart_args(after_update=True))
        except Exception as e:
            log.warning('update install failed: %r', e)
            with updater._lock:
                updater.state.update(status='error', error=f'Update failed: {e}')
    threading.Thread(target=run, daemon=True, name='update').start()
    return jsonify({'ok': True})

def _restart_args(after_update):
    """Command line for the updated program: the same arguments, plus where we came from."""
    args, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False; continue
        if a in ('--updated-from',):
            skip = True; continue
        if a in ('--after-update', '--no-browser', '--open'):
            continue
        args.append(a)
    args += ['--updated-from', __version__]
    if after_update:
        args += ['--after-update', '--no-browser']   # the open tab reloads itself
    return args

@app.route('/api/quit', methods=['POST'])
def api_quit():
    # JSON only: a plain cross-site form post can't trigger it.
    if not request.is_json:
        return jsonify({'error': 'JSON required'}), 400
    log.info('quit requested from the browser')
    threading.Timer(0.5, lambda: os._exit(0)).start()
    return jsonify({'ok': True})

# ── Settings ──────────────────────────────────────────────────────────────────

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        d = request.json or {}
        conn_fields = ('library_root', 'storage_username', 'storage_password', 'smb_port')
        before = {k: cfg.get(k) for k in conn_fields}
        for k in ('library_root', 'storage_username'):
            if k in d: cfg[k] = (d[k] or '').strip()
        if 'web_search' in d:
            cfg['web_search'] = bool(d['web_search'])
        if d.get('clear_app_password'):
            cfg['app_password_hash'] = ''
        elif d.get('app_password'):
            cfg['app_password_hash'] = config.hash_password(d['app_password'])
        _verified.clear()
        if 'allow_network' in d:
            cfg['allow_network'] = bool(d['allow_network'])
        for k in ('update_check', 'update_auto_install'):
            if k in d:
                cfg[k] = bool(d[k])
        if 'port' in d:
            try:
                p = int(d['port'])
                if 1 <= p <= 65535: cfg['port'] = p
            except (TypeError, ValueError): pass
        for k in config.SECRET_KEYS:
            if k == 'app_password_hash':
                continue   # only ever set from app_password above
            # Empty = keep the stored value; an explicit clear_<key> removes it.
            if d.get('clear_' + k):
                cfg[k] = ''
            elif d.get(k):
                cfg[k] = d[k] if k == 'storage_password' else d[k].strip()
        if 'smb_port' in d:
            try: cfg['smb_port'] = int(d['smb_port'])
            except (TypeError, ValueError): pass
        if 'ignored_folders' in d:
            cfg['ignored_folders'] = parse_patterns(d['ignored_folders'])
        try:
            if d.get('library_type'):
                config.apply_library_type(d['library_type'], d.get('naming'))
            elif d.get('naming'):
                cfg['library_type'] = 'custom'
                config.set_naming(d['naming'])
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        if any(cfg.get(k) != before[k] for k in conn_fields):
            storage.reset_all()   # connection settings changed: drop cached sessions
        save_cfg()
    return jsonify(_public())

def _public():
    out = public_cfg()
    out['settings_file'] = config.CFG_FILE
    out['version'] = __version__
    out['updated_from'] = _running.get('updated_from')
    out['running'] = {'network': not _auth['loopback_only'], 'port': _running['port'],
                      'password_from_cli': bool(_auth['password'])}
    return out

@app.route('/api/browse')
def api_browse():
    """
    Folder picker for the setup screen. No path: starting points (home folder,
    drives / volumes). Otherwise the subfolders of that folder, on any backend
    (using the saved credentials).
    """
    path = request.args.get('path', '').strip()
    if not path:
        places = [{'name': 'Home folder', 'path': os.path.expanduser('~')}]
        if os.name == 'nt':
            places += [{'name': f'{d}:\\', 'path': f'{d}:\\'} for d in string.ascii_uppercase
                       if os.path.exists(f'{d}:\\')]
        else:
            for base in ('/Volumes', '/mnt', '/media', f'/media/{os.environ.get("USER", "")}', '/run/media'):
                if os.path.isdir(base):
                    places += [{'name': os.path.join(base, n), 'path': os.path.join(base, n)}
                               for n in sorted(os.listdir(base))
                               if os.path.isdir(os.path.join(base, n)) and not n.startswith('.')]
            places.append({'name': '/', 'path': '/'})
        return jsonify({'path': '', 'parent': None, 'dirs': places, 'places': True})
    try:
        entries = storage.scandir(path)
    except Exception as e:
        return jsonify({'path': path, 'error': storage.error_message(e, path)}), 200
    dirs = sorted((e.name for e in entries if e.is_dir and not e.name.startswith('.')), key=str.lower)
    parent = storage.dirname(path)
    if storage.kind(path) == 'local':
        parent = os.path.dirname(os.path.normpath(path))
        if os.path.normpath(parent) == os.path.normpath(path):
            parent = ''   # filesystem root -> back to the starting points
    elif parent.count('/') < 3:   # smb://host or webdav://host: nothing above the share
        parent = None
    return jsonify({'path': path, 'parent': parent,
                    'dirs': [{'name': n, 'path': storage.join(path, n)} for n in dirs],
                    'flacs': sum(1 for e in entries if not e.is_dir and is_track(e.name))})

@app.route('/api/test-connection')
def api_test_connection():
    root = request.args.get('root', cfg.get('library_root', '')).strip()
    if not root:
        return jsonify({'ok': False, 'msg': 'No library path to test'})
    kind = storage.kind(root)
    try:
        storage.reset_all()   # force a fresh login with the current fields
        entries = storage.scandir(root)
        n = sum(1 for e in entries if e.is_dir and not is_ignored(e.name))
        return jsonify({'ok': True, 'kind': kind,
                        'msg': f'Connected ({kind}) — {n} folder(s) in {root}'})
    except Exception as e:
        return jsonify({'ok': False, 'kind': kind, 'msg': storage.error_message(e, root)})

# ── Library ───────────────────────────────────────────────────────────────────

@app.route('/api/library')
def api_library():
    # Filtered on the way out so an ignore-list change applies immediately,
    # even to a cache written before the folder was ignored. A cache from an
    # older version is not served: the client then rescans automatically.
    lib = _library()
    if lib and (lib.get('version') != SCHEMA_VERSION or lib.get('naming') != config.naming_key()):
        return jsonify({})   # outdated, or naming settings changed: the client rescans
    return jsonify(client_view(apply_ignore(lib)))

@app.route('/api/scan')
def api_scan():
    global _cached_lib
    root = request.args.get('root', cfg['library_root']).strip()
    if not root:
        return jsonify({'error': 'No library path configured'}), 400
    try:
        if not storage.isdir(root):
            return jsonify({'error': f'Not a directory: {root!r}'}), 400
    except Exception as e:
        return jsonify({'error': storage.error_message(e, root)}), 502

    prev = _library()
    if (not (prev and prev.get('root') == root) or prev.get('version') != SCHEMA_VERSION
            or prev.get('naming') != config.naming_key() or request.args.get('full') == '1'):
        prev = None   # full rescan: re-read every folder, ignore the cache

    errors, skipped = [], []
    try:
        artists = scan_library(root, prev['artists'] if prev else None, errors, skipped)
    except Exception as e:
        return jsonify({'error': storage.error_message(e, root)}), 502
    errors.sort(key=lambda e: e['path'])
    _cached_lib = {'root': root, 'artists': artists, 'scanned_at': time.time(), 'errors': errors,
                   'skipped': sorted(skipped), 'version': SCHEMA_VERSION, 'naming': config.naming_key()}
    _save_lib_cache(_cached_lib)
    return jsonify(client_view(_cached_lib))

# ── Search ────────────────────────────────────────────────────────────────────

def _page_arg():
    try: return max(0, int(request.args.get('page', 0)))
    except ValueError: return 0

@app.route('/api/search/album/fast')
def api_search_album_fast():
    """Fast official sources — shown immediately."""
    results, sources = run_providers('album', 'fast',
        (request.args.get('artist', ''), request.args.get('album', '')), _page_arg())
    return jsonify({'results': results, 'sources': sources})

@app.route('/api/search/album/slow')
def api_search_album_slow():
    """Slower sources (CAA, web) — merged in by the client when ready."""
    results, sources = run_providers('album', 'slow',
        (request.args.get('artist', ''), request.args.get('album', '')), _page_arg())
    return jsonify({'results': results, 'sources': sources})

@app.route('/api/search/artist')
def api_search_artist():
    page = _page_arg()
    results, sources = run_providers('artist', None, (request.args.get('name', ''),), page)
    return jsonify({'results': results, 'sources': sources, 'page': page})

# ── Tracks & images ───────────────────────────────────────────────────────────

_meta_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix='meta')

def _read_meta(fpath):
    try:
        audio = formats.open_audio(fpath)
        t = {}
        for k, v in formats.raw_pairs(audio, fpath):
            t.setdefault(tags.canonical(k), v)
        return bool(formats.pictures(audio, fpath)), t.get('TITLE', ''), t.get('TRACKNUMBER', '').split('/')[0]
    except Exception:
        return False, '', ''

@app.route('/api/album-tracks')
def api_album_tracks():
    album_path = request.args.get('path', '')
    if (err := _check_paths(album_path)): return err
    try:
        al_files, al_dirs = list_dir(album_path)
    except Exception as e:
        return jsonify({'error': storage.error_message(e, album_path)}), 502

    def tracks(dir_path, names):
        fpaths = [storage.join(dir_path, f) for f in sorted(names) if is_track(f)]
        metas = list(_meta_pool.map(_read_meta, fpaths))   # parallel: much faster over the network
        return [{'filename': storage.basename(fp), 'path': fp, 'has_art': m[0],
                 'title': m[1], 'track_num': m[2]} for fp, m in zip(fpaths, metas)]

    groups = []
    direct = tracks(album_path, al_files)
    if direct:
        groups.append({'disc_name': None, 'tracks': direct})
    else:
        for sub in sorted(al_dirs):
            sub_path = storage.join(album_path, sub)
            try: sub_files, _ = list_dir(sub_path)
            except Exception: continue
            t = tracks(sub_path, sub_files)
            if t:
                groups.append({'disc_name': sub, 'tracks': t})
    return jsonify({'groups': groups, 'total': sum(len(g['tracks']) for g in groups)})

@app.route('/api/track-art')
def api_track_art():
    fpath = request.args.get('file', '')
    if not is_track(storage.basename(fpath)) or not _in_library(fpath):
        return '', 404
    try:
        pics = track_pictures(fpath)
        if pics:
            p = next((x for x in pics if x['type'] == 3), pics[0])
            return Response(p['data'], mimetype=p['mime'] or 'image/jpeg')
    except Exception:
        pass
    return '', 404

@app.route('/api/image/local')
def api_local_img():
    p = request.args.get('path', '')
    from .scan import IMAGE_EXTS
    if not p.lower().endswith(IMAGE_EXTS) or not _in_library(p):
        return '', 400
    try:
        data = storage.read_bytes(p)
    except Exception:
        return '', 404
    mime = sniff_image(data) or 'image/jpeg'          # by content: cover.jpg may really be a PNG
    if mime in ('image/bmp', 'image/tiff'):           # browsers can't show TIFF
        try:
            data, mime = to_jpeg(data, mime), 'image/jpeg'
        except ValueError:
            return '', 415
    return Response(data, mimetype=mime)

@app.route('/api/image/redacted')
def api_redacted_image():
    """An image on Redacted's image host, fetched with credentials that never reach the browser."""
    from . import redacted
    try:
        data, ct = redacted.fetch_site_image(request.args.get('path', ''))
    except redacted.RedactedError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        return '', 404
    return Response(data, mimetype=ct if ct.startswith('image/') else 'image/jpeg')

@app.route('/api/image/embedded')
def api_embedded():
    album_path = request.args.get('path', '')
    try:
        disc_info = json.loads(request.args.get('disc_info', '[]'))
    except ValueError:
        disc_info = []
    if not _in_library(album_path) or not all(_in_library(d.get('path', '')) for d in disc_info):
        return jsonify({'has_art': False})
    data, mime = find_album_embedded(album_path, disc_info)
    if data:
        return jsonify({'has_art': True, 'data_url': to_data_url(data, mime), 'mime': mime})
    return jsonify({'has_art': False})

@app.route('/api/set-artist-img', methods=['POST'])
def api_set_artist_img():
    d = request.json or {}
    if (err := _check_paths(d.get('artist_path', ''))): return err
    try:
        if d.get('image_data'):
            data, mime = decode_uploaded_image(d['image_data'])
        else:
            data, mime = dl_image(d['image_url'])
        p = write_artist_img(d['artist_path'], data, mime)
        return jsonify({'success': True, 'path': p, 'data_url': to_data_url(data, mime)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── File check ────────────────────────────────────────────────────────────────

@app.route('/api/check', methods=['GET', 'POST'])
def api_check():
    """POST: start checking the whole scanned library. GET: running job (if any) and the last report."""
    if request.method == 'POST':
        lib = _library()
        if not lib or not lib.get('artists'):
            return jsonify({'error': 'Scan the library first'}), 400
        d = request.json or {}
        job = check.start_job(apply_ignore(lib), deep=bool(d.get('deep')))
        return jsonify(job.progress())
    job = check.running_job()
    return jsonify({'running': job.progress() if job else None, 'report': check.last_report(),
                    'deep_available': check.deep_check_available()})

@app.route('/api/check/<job_id>', methods=['GET', 'DELETE'])
def api_check_job(job_id):
    job = check.get_job(job_id)
    if not job:
        return jsonify({'error': 'Unknown check'}), 404
    if request.method == 'DELETE':
        job.cancelled = True
    return jsonify(job.progress())

@app.route('/api/check-album', methods=['POST'])
def api_check_album():
    d = request.json or {}
    album = {'name': d.get('name', ''), 'path': d.get('path', ''), 'disc_info': d.get('disc_info') or []}
    if (err := _check_paths(album['path'], *[x.get('path', '') for x in album['disc_info']])):
        return err
    res = check.check_album(album, deep=bool(d.get('deep')))
    check._save_cache()
    return jsonify(res)

# ── Bulk fix ──────────────────────────────────────────────────────────────────

_fix_jobs = {}

class _FixJob:
    def __init__(self, albums, embed, use_embedded, tidy=False, remove_old=True):
        self.id = uuid.uuid4().hex[:12]
        self.albums, self.embed, self.use_embedded = albums, embed, use_embedded
        self.tidy = tidy   # only convert cover.png / cover.jpeg / ... into the right file
        self.remove_old = remove_old
        self.done, self.tracks, self.current, self.status = 0, 0, '', 'running'
        self.cancelled = False
        self.results = []

    def progress(self):
        c = {}
        for r in self.results:
            c[r['status']] = c.get(r['status'], 0) + 1
        return {'id': self.id, 'status': self.status, 'done': self.done, 'total': len(self.albums),
                'tracks': self.tracks, 'current': self.current, 'counts': c,
                'results': self.results if self.status != 'running' else None}

    def run(self):
        for al in self.albums:
            if self.cancelled:
                break
            self.current = al.get('label') or al['path']
            try:
                if self.tidy:
                    t = tidy_artist(al['path'], self.remove_old) if al.get('artist') else tidy_album(al, self.remove_old)
                    r = dict(t, status='fixed' if t['written'] or t['removed'] else 'skipped', embedded=0,
                             message='' if t['written'] or t['removed'] else 'nothing to change')
                else:
                    r = fix_album_covers(al, embed=self.embed, use_embedded=self.use_embedded,
                                         on_track=lambda _p: setattr(self, 'tracks', self.tracks + 1),
                                         remove_old=self.remove_old)
            except Exception as e:
                r = {'status': 'error', 'written': [], 'embedded': 0, 'message': storage.error_message(e, al['path'])}
            r['path'] = al['path']; r['label'] = al.get('label', '')
            self.results.append(r)
            self.done += 1
        self.status = 'cancelled' if self.cancelled else 'done'

@app.route('/api/fix-covers', methods=['POST'])
def api_fix_covers():
    """Bulk: give every album/disc folder in the list a cover.jpg (never overwriting one)."""
    d = request.json or {}
    albums = [{'path': a.get('path', ''), 'disc_info': a.get('disc_info') or [], 'label': a.get('label', ''),
               'artist': bool(a.get('artist'))} for a in d.get('albums', [])]
    for a in albums:
        if (err := _check_paths(a['path'], *[x.get('path', '') for x in a['disc_info']])):
            return err
    if any(j.status == 'running' for j in _fix_jobs.values()):
        return jsonify({'error': 'A fix is already running'}), 409
    job = _FixJob(albums, embed=bool(d.get('embed', True)), use_embedded=bool(d.get('use_embedded', True)),
                  tidy=d.get('mode') == 'tidy', remove_old=bool(d.get('remove_old', True)))
    _fix_jobs[job.id] = job
    threading.Thread(target=job.run, daemon=True, name='fix').start()
    return jsonify(job.progress())

@app.route('/api/fix-covers/<job_id>', methods=['GET', 'DELETE'])
def api_fix_job(job_id):
    job = _fix_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Unknown job'}), 404
    if request.method == 'DELETE':
        job.cancelled = True
    return jsonify(job.progress())

# ── Tags ──────────────────────────────────────────────────────────────────────

_FORMAT_WORD = re.compile(r'^(flac|mp3|aac|alac|ogg|opus|web|webdl|lossless|hi-?res|v0|v2|320|256|'
                          r'\d{2}(bit|b)?|\d{2,3}(\.\d)?k(hz)?|\d{2}-\d{2,3}(\.\d)?|\d+bit|\d{2}bit-\d{2,3}(khz)?)$', re.I)

def edition_hint(folder_name):
    """'Album (2013 Remaster) [FLAC 24-96] (1994)' -> '2013 Remaster': bracket parts that describe an edition."""
    keep = []
    for part in re.findall(r'[\(\[\{]([^\)\]\}]+)[\)\]\}]', folder_name):
        p = part.strip()
        words = [w for w in re.split(r'[\s,/_]+', p) if w]
        if not words or re.fullmatch(r'\d{4}', p) or all(_FORMAT_WORD.match(w) for w in words):
            continue   # a bare year or only format info (FLAC 24-96, WEB, 320, ...)
        keep.append(p)
    return ', '.join(keep)

def base_title(folder_name):
    return re.sub(r'\s+', ' ', re.sub(r'[\(\[\{][^\)\]\}]*[\)\]\}]', '', folder_name)).strip().casefold()

def _album_arg(d):
    album = {'name': d.get('name') or storage.basename(d.get('path', '')), 'path': d.get('path', ''),
             'disc_info': d.get('disc_info') or []}
    err = _check_paths(album['path'], *[x.get('path', '') for x in album['disc_info']])
    return album, err

def _album_tracks(album):
    """Local tracks in play order: [{'path', 'name', 'folder': index of its disc folder}]."""
    folders = [album['path']] + [x['path'] for x in album['disc_info'] if x.get('path') != album['path']]
    out = []
    base = album['path'].rstrip('/\\')
    for i, folder in enumerate(folders):
        files, _ = list_dir(folder)
        for name in sorted(files, key=str.lower):
            if is_track(name):
                p = storage.join(folder, name)
                out.append({'path': p, 'name': p[len(base) + 1:].replace('\\', '/'), 'folder': i})
    return out

def map_tracks(local, remote):
    """
    Pair local tracks with release tracks. -> ([remote index or None per local track], how).
    1. by the disc/track numbers already in the files, when they're complete and unique;
    2. folder by folder (CD 1 -> disc 1, ...) when the counts of discs match;
    3. in order.
    """
    idx = {(t['disc'], t['position']): i for i, t in enumerate(remote)}
    pairs = []
    for t in local:
        tn = (t['tags'].get('TRACKNUMBER') or [''])[0].split('/')[0].strip()
        dn = (t['tags'].get('DISCNUMBER') or ['1'])[0].split('/')[0].strip() or '1'
        pairs.append((int(dn), int(tn)) if tn.isdigit() and dn.isdigit() else None)
    if all(pairs) and len(set(pairs)) == len(pairs) and all(p in idx for p in pairs):
        return [idx[p] for p in pairs], 'by the track and disc numbers in your files'
    folders = sorted({t['folder'] for t in local})
    discs = sorted({t['disc'] for t in remote})
    if len(folders) > 1 and len(folders) == len(discs):
        out, seen = [], {}
        for t in local:
            fi = folders.index(t['folder'])
            k = seen[fi] = seen.get(fi, -1) + 1          # k-th track of this folder
            same = [i for i, r in enumerate(remote) if r['disc'] == discs[fi]]
            out.append(same[k] if k < len(same) else None)
        return out, 'disc folder by disc folder, in file-name order'
    return [i if i < len(remote) else None for i in range(len(local))], 'in file-name order'

@app.route('/api/tags/read', methods=['POST'])
def api_tags_read():
    album, err = _album_arg(request.json or {})
    if err: return err
    try:
        tracks = _album_tracks(album)
    except Exception as e:
        return jsonify({'error': storage.error_message(e, album['path'])}), 502
    def one(t):
        try:
            raw = tags.read_raw(t['path'])
        except Exception as e:
            return dict(t, error=storage.error_message(e, t['path']), raw=[], tags={}, notes=[], problems=[])
        norm, notes = tags.normalize(raw)
        return dict(t, raw=raw, tags=norm, notes=notes,
                    problems=[{'level': l, 'code': c, 'msg': m} for l, c, m in tags.tag_problems(raw)])
    out = list(_meta_pool.map(one, tracks))
    siblings = []
    lib = _library() or {}
    for a in lib.get('artists', []):
        if any(al['path'] == album['path'] for al in a['albums']):
            siblings = [{'name': al['name'], 'path': al['path'], 'hint': edition_hint(al['name'])}
                        for al in a['albums'] if al['path'] != album['path']
                        and base_title(al['name']) == base_title(album['name'])]
    return jsonify({'tracks': out, 'edition_hint': edition_hint(album['name']), 'siblings': siblings,
                    'backups': tags.list_backups(album['path'])[:10],
                    'redacted': bool(cfg.get('redacted_key'))})

@app.route('/api/tags/lookup', methods=['POST'])
def api_tags_lookup():
    d = request.json or {}
    try:
        if d.get('source') == 'redacted':
            from . import redacted
            if not redacted.enabled():
                return jsonify({'error': 'Add your Redacted API key in Settings first'}), 400
            return jsonify({'results': redacted.search_groups(d.get('artist', ''), d.get('album', ''))})
        from . import musicbrainz
        return jsonify({'results': musicbrainz.search_releases(d.get('artist', ''), d.get('album', ''),
                                                               d.get('track_count'), d.get('hint', ''))})
    except Exception as e:
        return jsonify({'error': f'Lookup failed: {e}'}), 502

@app.route('/api/tags/release', methods=['POST'])
def api_tags_release():
    """Proposed tags for every local track from one MusicBrainz release / Redacted group."""
    d = request.json or {}
    album, err = _album_arg(d)
    if err: return err
    try:
        local = _album_tracks(album)
        for t in local:
            try: t['tags'] = tags.normalize(tags.read_raw(t['path']))[0]
            except Exception: t['tags'] = {}
        if d.get('source') == 'redacted':
            from . import redacted
            g = redacted.group_tags(d['id'], [storage.basename(t['path']) for t in local])
            return jsonify(dict(g, source='redacted'))
        from . import musicbrainz
        rel = musicbrainz.release_tags(d['id'])
    except Exception as e:
        return jsonify({'error': f'Lookup failed: {e}'}), 502
    mapping, how = map_tracks(local, rel['tracks'])
    proposals = []
    for t, ri in zip(local, mapping):
        r = rel['tracks'][ri] if ri is not None else None
        proposals.append({'path': t['path'], 'name': t['name'], 'matched': r is not None,
                          'remote': {'disc': r['disc'], 'position': r['position'], 'length': r['length']} if r else None,
                          'tags': dict(rel['album'], **(r['tags'] if r else {}))})
    note = ''
    if len(local) != len(rel['tracks']):
        note = f'The release has {len(rel["tracks"])} tracks, the folder has {len(local)} — check the pairing.'
    return jsonify({'source': 'musicbrainz', 'summary': rel['summary'], 'edition': rel['edition'],
                    'how': how, 'note': note, 'proposals': proposals})

@app.route('/api/tags/apply', methods=['POST'])
def api_tags_apply():
    """Write complete new tags per file (a backup of the old ones is kept for undo)."""
    if not request.is_json:
        return jsonify({'error': 'JSON required'}), 400
    d = request.json
    changes = [{'path': c['path'], 'tags': {tags.canonical(k): [str(v) for v in (vals if isinstance(vals, list) else [vals]) if str(v).strip()]
                                            for k, vals in (c.get('tags') or {}).items()}}
               for c in d.get('changes', [])]
    if (err := _check_paths(*[c['path'] for c in changes])):
        return err
    if not all(is_track(storage.basename(c['path'])) for c in changes):
        return jsonify({'error': 'Only music files can be tagged'}), 400
    try:
        backup, errors = tags.apply(changes, label=d.get('label', 'Tag edit'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'backup': backup, 'errors': errors, 'written': len(changes) - len(errors)})

@app.route('/api/tags/apply-raw', methods=['POST'])
def api_tags_apply_raw():
    """One file, exactly the given FIELD=value pairs (Advanced). Backed up like any other change."""
    if not request.is_json:
        return jsonify({'error': 'JSON required'}), 400
    d = request.json
    path, pairs = d.get('path', ''), [(str(k), str(v)) for k, v in d.get('pairs', [])]
    if (err := _check_paths(path)):
        return err
    if not is_track(storage.basename(path)):
        return jsonify({'error': 'Not a music file'}), 400
    if formats.family(path) in ('flac', 'vorbis'):   # Vorbis comment names: printable ASCII without '='
        bad = [k for k, _ in pairs if not k or not all(0x20 <= ord(c) <= 0x7D and c != '=' for c in k)]
        hint = 'plain letters, digits, spaces, _ only'
    else:                                            # TXXX:..., ©nam, WM/..., ...
        bad = [k for k, _ in pairs if not k or len(k) > 255 or any(ord(c) < 0x20 or c == '\x7f' for c in k)]
        hint = 'no control characters'
    if bad:
        return jsonify({'error': f'Invalid field name: {bad[0]!r} ({hint})'}), 400
    backup, errors = tags.apply([{'path': path, 'tags': pairs}], label=f'Raw edit: {storage.basename(path)}')
    return jsonify({'backup': backup, 'errors': errors})

@app.route('/api/tags/undo', methods=['POST'])
def api_tags_undo():
    if not request.is_json:
        return jsonify({'error': 'JSON required'}), 400
    try:
        b, errors = tags.undo((request.json or {}).get('id', ''))
    except (ValueError, OSError) as e:
        return jsonify({'error': f'Cannot undo: {e}'}), 400
    if (err := _check_paths(*[x['path'] for x in b['files']])):
        return err
    return jsonify({'restored': len(b['files']) - len(errors), 'errors': errors, 'label': b.get('label', '')})

_cleanup_jobs = {}

class _CleanupJob:
    def __init__(self, albums):
        self.id = uuid.uuid4().hex[:12]
        self.albums, self.done, self.files, self.status, self.errors = albums, 0, 0, 'running', []
        self.backups, self.current = [], ''

    def progress(self):
        return {'id': self.id, 'status': self.status, 'done': self.done, 'total': len(self.albums),
                'files': self.files, 'current': self.current, 'errors': self.errors[:50], 'backups': self.backups}

    def run(self):
        for al in self.albums:
            self.current = al.get('label') or al['path']
            try:
                changes = []
                for t in _album_tracks(al):
                    raw = tags.read_raw(t['path'])
                    norm, _ = tags.normalize(raw)
                    if tags.as_pairs(norm) != raw:
                        changes.append({'path': t['path'], 'tags': norm})
                if changes:
                    bid, errs = tags.apply(changes, label=f'Clean up tags: {self.current}')
                    self.backups.append(bid); self.errors += errs
                    self.files += len(changes) - len(errs)
            except Exception as e:
                self.errors.append({'path': al['path'], 'error': storage.error_message(e, al['path'])})
            self.done += 1
        self.status = 'done'

@app.route('/api/tags/cleanup', methods=['POST'])
def api_tags_cleanup():
    """Bulk: normalise field names / duplicates in every listed album (backups kept)."""
    d = request.json or {}
    albums = []
    for a in d.get('albums', []):
        al, err = _album_arg(a)
        if err: return err
        al['label'] = a.get('label', '')
        albums.append(al)
    job = _CleanupJob(albums)
    _cleanup_jobs[job.id] = job
    threading.Thread(target=job.run, daemon=True, name='tag-cleanup').start()
    return jsonify(job.progress())

@app.route('/api/tags/cleanup/<job_id>')
def api_tags_cleanup_job(job_id):
    job = _cleanup_jobs.get(job_id)
    return jsonify(job.progress()) if job else (jsonify({'error': 'Unknown job'}), 404)

# ── Embed jobs ────────────────────────────────────────────────────────────────

_jobs = {}
_jobs_lock = threading.Lock()

@app.route('/api/prepare-embed', methods=['POST'])
def api_prepare_embed():
    d = request.json or {}
    album_path = d.get('album_path', '')
    disc_info = d.get('disc_info') or []
    source = d.get('source', 'url')
    if (err := _check_paths(album_path, *[x.get('path', '') for x in disc_info],
                            *([d.get('cover_path') or ''] if source == 'cover_file' else []))):
        return err
    try:
        if source == 'cover_file':
            cover = d.get('cover_path')
            if not storage.isfile(cover):
                return jsonify({'error': 'Cover file not found'}), 400
            img = storage.read_bytes(cover)
            mime = 'image/png' if cover.lower().endswith('.png') else 'image/jpeg'
        elif source == 'embedded':
            img, mime = find_album_embedded(album_path, disc_info)
            if not img:
                return jsonify({'error': 'No embedded art found'}), 400
        elif source == 'upload':
            img, mime = decode_uploaded_image(d.get('image_data'))
        else:
            img, mime = dl_image(d['image_url'])

        # One JPEG for everything: cover.jpg in the album folder and in every
        # disc folder, and the same picture embedded in every track.
        img, mime = to_jpeg(img, mime), 'image/jpeg'
        saved_cover, disc_covers = write_covers(album_path, disc_info, img)

        # embedding can be switched off in Settings (then only the cover files are written)
        all_flacs = collect_album_flacs(disc_info or [{'path': album_path}]) if cfg.get('embed_art', True) else []
        job_id = str(uuid.uuid4())
        with _jobs_lock:
            _jobs[job_id] = {'flacs': all_flacs, 'img': img, 'mime': mime}
        return jsonify({'success': True, 'embed_id': job_id, 'data_url': to_data_url(img, mime),
                        'cover_path': saved_cover, 'disc_covers': disc_covers,
                        'total_tracks': len(all_flacs)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/embed/<job_id>')
def api_embed(job_id):
    with _jobs_lock:
        job = _jobs.pop(job_id, None)   # a job runs once
    if not job:
        return jsonify({'error': 'Unknown job'}), 404

    def stream():
        flacs, img, mime = job['flacs'], job['img'], job['mime']
        total = len(flacs)
        yield f"data: {json.dumps({'status': 'start', 'total': total})}\n\n"
        errors = []
        for i, fpath in enumerate(flacs):
            fname = storage.basename(fpath)
            try:
                embed_into(fpath, img, mime)
            except Exception as e:
                errors.append({'file': fname, 'error': str(e)})
            pct = round((i + 1) / total * 100)
            yield f"data: {json.dumps({'status': 'progress', 'current': i + 1, 'total': total, 'pct': pct, 'file': fname})}\n\n"
        yield f"data: {json.dumps({'status': 'done', 'errors': errors})}\n\n"

    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# ── CLI ───────────────────────────────────────────────────────────────────────

def _free_port(host, start):
    """The first port from `start` on that we can listen on (macOS uses 5000 for AirPlay)."""
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name != 'nt':
                # like the server itself: connections lingering in TIME_WAIT after
                # a restart must not make the port look taken
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return start

def _lan_ip():
    """This computer's address on the local network (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('192.0.2.1', 9))   # TEST-NET address: only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return '<this computer\'s IP>'

def prepare(argv=None):
    """Parse the command line, remember what was given, work out host/port. -> (host, port, wanted, open_browser)"""
    load_cfg()
    frozen = getattr(sys, 'frozen', False)   # the standalone app (PyInstaller)
    parser = argparse.ArgumentParser(prog='art-manager', description='Art Manager — artwork and tags for a music library')
    parser.add_argument('library', nargs='?',
                        help='Local path, smb://host/share/path, or webdav(s)://host/path (saved)')
    def port_number(v):
        p = int(v)
        if not 1 <= p <= 65535:
            raise argparse.ArgumentTypeError('must be between 1 and 65535')
        return p
    parser.add_argument('--port', type=port_number, default=None,
                        help='Port (default: from Settings, 5000; the next free one is used if taken)')
    parser.add_argument('--host', default=None,
                        help='Interface to listen on, e.g. 0.0.0.0 (default: from Settings — '
                             'this computer only unless "Allow other devices" is on)')
    parser.add_argument('--password', default=None,
                        help='Require this password for this run (also ARTMGR_PASSWORD). '
                             'To save one, use Settings.')
    parser.add_argument('--fanart', help='Fanart.tv API key (saved)')
    parser.add_argument('--user', '--smb-user', dest='user', help='Username for SMB/WebDAV (saved)')
    parser.add_argument('--pass', '--smb-pass', dest='pw', help='Password for SMB/WebDAV (saved)')
    parser.add_argument('--open', dest='open_browser', action='store_true', default=None,
                        help='Open the browser on start (default for the standalone app)')
    parser.add_argument('--no-browser', dest='open_browser', action='store_false',
                        help="Don't open the browser")
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--updated-from', default=None, help=argparse.SUPPRESS)
    parser.add_argument('--after-update', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    _running['updated_from'] = args.updated_from

    # Anything given on the command line is remembered, so it only has to be given once.
    given = {'library_root': args.library, 'fanart_key': args.fanart,
             'storage_username': args.user, 'storage_password': args.pw}
    given = {k: v for k, v in given.items() if v}
    if given:
        cfg.update(given)
        save_cfg()

    host = args.host or os.environ.get('ARTMGR_HOST') or \
        ('0.0.0.0' if cfg.get('allow_network') else '127.0.0.1')
    env_port = 0
    if os.environ.get('ARTMGR_PORT'):
        try:
            env_port = port_number(os.environ['ARTMGR_PORT'])
        except (ValueError, argparse.ArgumentTypeError):
            print(f"  Ignoring ARTMGR_PORT={os.environ['ARTMGR_PORT']!r}: not a port number (1-65535)")
    wanted = args.port or env_port or int(cfg.get('port') or 5000)
    if args.after_update:
        # Started by an update of the running app: wait for the old one to let go of the port.
        for _ in range(200):
            if not _art_manager_at(wanted):
                break
            time.sleep(0.1)
    port = _free_port(host, wanted)

    _auth['password'] = args.password or os.environ.get('ARTMGR_PASSWORD') or None
    _auth['loopback_only'] = _is_loopback(host)
    _running.update(host=host, port=port)
    want_browser = args.open_browser if args.open_browser is not None else frozen
    return host, port, wanted, want_browser


def _setup_logging():
    """Always log to <data dir>/art-manager.log (startup, errors); ARTMGR_DEBUG adds detail on screen."""
    log.setLevel(logging.DEBUG)
    if getattr(log, '_artmgr_configured', False):
        return
    log._artmgr_configured = True
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(os.path.join(config.DATA_DIR, 'art-manager.log'),
                                 maxBytes=1_000_000, backupCount=1, encoding='utf-8')
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        log.addHandler(fh)
    except OSError:
        pass
    if os.environ.get('ARTMGR_DEBUG'):
        sh = logging.StreamHandler()
        sh.setLevel(logging.DEBUG if os.environ['ARTMGR_DEBUG'] == '2' else logging.INFO)
        sh.setFormatter(logging.Formatter('%(asctime)s %(name)s: %(message)s'))
        log.addHandler(sh)

def _fix_child_env():
    """
    The Linux standalone app points LD_LIBRARY_PATH at its own bundled
    libraries; a browser started from it would inherit that and can fail
    silently. Put the user's original value back for child processes.
    """
    if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
        orig = os.environ.pop('LD_LIBRARY_PATH_ORIG', None)
        if orig is not None:
            os.environ['LD_LIBRARY_PATH'] = orig
        else:
            os.environ.pop('LD_LIBRARY_PATH', None)

def _art_manager_at(port):
    """True if an Art Manager already answers on this port of this computer."""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/ping', timeout=1.5) as r:
            return json.load(r).get('app') == 'art-manager'
    except Exception:
        return False

def open_browser(url):
    """Open url in the user's browser. Returns True if a browser was started."""
    try:
        if sys.platform == 'win32':
            os.startfile(url)
            return True
        if sys.platform == 'darwin':
            return subprocess.call(['open', url]) == 0
        for cmd in (['xdg-open', url], ['gio', 'open', url]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except FileNotFoundError:
                continue
    except Exception as e:
        log.info('could not open the browser directly: %r', e)
    try:
        return webbrowser.open(url)
    except Exception as e:
        log.info('webbrowser failed: %r', e)
        return False

def _open_when_ready(port, url):
    """Wait until the server accepts connections, then open the browser."""
    for _ in range(150):
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.1)
    if open_browser(url):
        print(f"  Opened {url} in your browser.\n", flush=True)
    else:
        print(f"  Couldn't open a browser automatically — open {url} yourself.\n", flush=True)

def _pause_before_exit():
    """Keep the standalone app's window open so a message can be read."""
    if getattr(sys, 'frozen', False):
        try:
            if sys.stdin and sys.stdin.isatty():
                input('\nPress Enter to close this window.')
            else:
                time.sleep(15)
        except Exception:
            pass

def main(argv=None):
    # A Windows console or a redirected log may not handle "→" etc.: never crash on it.
    for stream in (sys.stdout, sys.stderr):
        try: stream.reconfigure(errors='replace')
        except Exception: pass
    _fix_child_env()
    _setup_logging()
    try:
        _main(argv)
    except SystemExit:
        raise
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            return
        msg = traceback.format_exc()
        log.error('Art Manager stopped with an error:\n%s', msg)
        print('\nArt Manager stopped with an error:\n')
        print(msg)
        print(f'This is also saved in {os.path.join(config.DATA_DIR, "art-manager.log")}')
        _pause_before_exit()
        sys.exit(1)

def _install_staged_update():
    """Standalone app: an update downloaded earlier is installed before starting."""
    if updater.install_kind() != 'app':
        return
    staged = updater.staged_update()
    if staged and cfg.get('update_auto_install', True) and updater.can_replace_program():
        print(f"\nInstalling the update that was downloaded ({os.path.basename(staged)})…", flush=True)
        try:
            updater.apply(staged, _restart_args(after_update=False))
        except Exception as e:
            log.warning('installing the staged update failed: %r', e)
            print(f"  Couldn't install it ({e}); starting the current version.", flush=True)

def _main(argv):
    load_cfg()
    args_seen = argv or sys.argv[1:]
    if updater.install_kind() == 'app' and not any(a in ('--version', '-h', '--help') for a in args_seen):
        updater.cleanup()   # leftovers from an earlier update (also right after one)
    if not any(a in ('--after-update', '--version', '-h', '--help') for a in args_seen):
        _install_staged_update()
    host, port, wanted, want_browser = prepare(argv)
    frozen = getattr(sys, 'frozen', False)
    if _running.get('updated_from'):
        print(f"\n  Updated from {_running['updated_from']} to {__version__}. Your settings are unchanged.")
    threading.Thread(target=updater.background, daemon=True, name='update-check').start()

    # Double-clicked again while it's already running: just show it.
    if port != wanted and _art_manager_at(wanted):
        url = f"http://localhost:{wanted}"
        print(f"\nArt Manager is already running at {url}")
        if want_browser:
            open_browser(url)
            print("  Opened it in your browser.")
        if frozen:
            time.sleep(4)
        return

    url = f"http://localhost:{port}"
    root = cfg['library_root'] or ''
    log.info('starting %s on %s:%s, library %r', __version__, host, port, root)
    print(f"\nArt Manager {__version__}  →  {url}")
    if port != wanted:
        print(f"  (port {wanted} is in use, using {port})")
    print(f"  Library : {root or '(not set — choose one in the browser)'}" + (f"  [{storage.kind(root)}]" if root else ''))
    if root and storage.kind(root) != 'local':
        print(f"  User    : {cfg.get('storage_username') or '(none)'}")
    print(f"  Settings: {config.CFG_FILE}")
    if not _is_loopback(host):
        lan_ip = _lan_ip()
        print(f"  Network : other devices can open http://{lan_ip}:{port}")
        if not _password_required():
            print("  WARNING : no app password set — ANYONE on your network can change your library.")
            print("            Set one in Settings → Access.")
    if frozen:
        print("\n  Keep this window open while you use Art Manager; close it (or use")
        print("  Settings → Quit) to stop.")
    print()

    if want_browser:
        threading.Thread(target=_open_when_ready, args=(port, url), daemon=True).start()
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
