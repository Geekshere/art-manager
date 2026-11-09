"""Flask app: HTTP API, embed jobs (SSE), access control and the CLI entry point."""
import argparse, concurrent.futures, hmac, json, os, posixpath, threading, time, uuid

from flask import Flask, Response, jsonify, render_template, request

from . import __version__, config, storage
from .artwork import (collect_album_flacs, decode_uploaded_image, dl_image, embed_into,
                      find_album_embedded, read_flac, to_data_url, to_jpeg, write_artist_img,
                      write_covers)
from .config import cfg, is_ignored, load_cfg, parse_patterns, public_cfg, save_cfg
from .scan import SCHEMA_VERSION, apply_ignore, client_view, list_dir, scan_library
from .search import run_providers

app = Flask(__name__)

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

_auth = {'password': None, 'loopback_only': False}   # set at startup from CLI flags
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
    pw = _auth['password']
    if not pw:
        return None
    a = request.authorization
    if a and a.password is not None and hmac.compare_digest(a.password.encode(), pw.encode()):
        return None
    return Response('Password required', 401, {'WWW-Authenticate': 'Basic realm="Art Manager"'})

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
        for k in config.SECRET_KEYS:
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
        if any(cfg.get(k) != before[k] for k in conn_fields):
            storage.reset_all()   # connection settings changed: drop cached sessions
        save_cfg()
    return jsonify(public_cfg())

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
    if lib and lib.get('version') != SCHEMA_VERSION:
        return jsonify({})
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
            or request.args.get('full') == '1'):
        prev = None   # full rescan: re-read every folder, ignore the cache

    errors = []
    try:
        artists = scan_library(root, prev['artists'] if prev else None, errors)
    except Exception as e:
        return jsonify({'error': storage.error_message(e, root)}), 502
    errors.sort(key=lambda e: e['path'])
    _cached_lib = {'root': root, 'artists': artists, 'scanned_at': time.time(), 'errors': errors,
                   'version': SCHEMA_VERSION}
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
        audio = read_flac(fpath)
        title = tn = ''
        if audio.tags:
            title = audio.tags.get('title', [''])[0]
            tn = audio.tags.get('tracknumber', [''])[0]
        return bool(audio.pictures), title, tn
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
        fpaths = [storage.join(dir_path, f) for f in sorted(names) if f.lower().endswith('.flac')]
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
    if not fpath.lower().endswith('.flac') or not _in_library(fpath):
        return '', 404
    try:
        audio = read_flac(fpath)
        if audio.pictures:
            return Response(audio.pictures[0].data, mimetype=audio.pictures[0].mime)
    except Exception:
        pass
    return '', 404

@app.route('/api/image/local')
def api_local_img():
    p = request.args.get('path', '')
    if not p.lower().endswith(('.jpg', '.jpeg', '.png')) or not _in_library(p):
        return '', 400
    try:
        data = storage.read_bytes(p)
    except Exception:
        return '', 404
    return Response(data, mimetype='image/png' if p.lower().endswith('.png') else 'image/jpeg')

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

        all_flacs = collect_album_flacs(disc_info or [{'path': album_path}])
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

def main(argv=None):
    load_cfg()
    parser = argparse.ArgumentParser(prog='art-manager', description='Art Manager — artwork for a FLAC library')
    parser.add_argument('library', nargs='?',
                        help='Local path, smb://host/share/path, or webdav(s)://host/path')
    parser.add_argument('--port', type=int, default=int(os.environ.get('ARTMGR_PORT', 5000)))
    parser.add_argument('--host', default=os.environ.get('ARTMGR_HOST', '127.0.0.1'),
                        help='Interface to listen on (default 127.0.0.1 = this machine only). '
                             'Use 0.0.0.0 to allow other devices on your network.')
    parser.add_argument('--password', default=os.environ.get('ARTMGR_PASSWORD'),
                        help='Require this password (HTTP basic auth, any username). '
                             'Also read from ARTMGR_PASSWORD. Recommended with --host.')
    parser.add_argument('--fanart', help='Fanart.tv API key')
    parser.add_argument('--user', '--smb-user', dest='user', help='Username for SMB/WebDAV')
    parser.add_argument('--pass', '--smb-pass', dest='pw',
                        help='Password for SMB/WebDAV (prefer ARTMGR_STORAGE_PASSWORD: '
                             'command-line arguments are visible to other users)')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = parser.parse_args(argv)

    if args.library: cfg['library_root'] = args.library
    if args.fanart:  cfg['fanart_key'] = args.fanart
    if args.user:    cfg['storage_username'] = args.user
    if args.pw:      cfg['storage_password'] = args.pw

    _auth['password'] = args.password or None
    _auth['loopback_only'] = _is_loopback(args.host)
    shown = 'localhost' if _is_loopback(args.host) else args.host
    root = cfg['library_root'] or ''
    print(f"\nArt Manager {__version__}  →  http://{shown}:{args.port}")
    print(f"  Library : {root or '(not set)'}" + (f"  [{storage.kind(root)}]" if root else ''))
    if storage.kind(root) != 'local':
        print(f"  User    : {cfg.get('storage_username') or '(none)'}")
    print(f"  Config  : {config.CFG_FILE}")
    if not _is_loopback(args.host):
        print()
        print("  WARNING : listening on", args.host, "— other devices on your network can reach this app")
        print("            and modify your library." + ("" if args.password else
              " No --password set: ANYONE who can reach this port has full access."))
    print()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
