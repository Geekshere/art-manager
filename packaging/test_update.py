"""
Release check: an older standalone build updates itself to the new one.

    python packaging/test_update.py <old program> <new release asset> <expected new version>

Serves the new asset as a fake GitHub release on localhost, starts the old
program pointed at it, saves a setting, triggers "Update now" through the
API, and waits until the same port answers with the new version and the
setting is still there. Runs on Windows, macOS and Linux in release.yml.
"""
import hashlib, http.server, json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request

old_prog, asset, expected = sys.argv[1], sys.argv[2], sys.argv[3]
work = tempfile.mkdtemp(prefix='artmgr-update-test-')
rel_dir = os.path.join(work, 'release')
os.makedirs(os.path.join(rel_dir, 'dl'))
asset_name = os.path.basename(asset)
shutil.copy(asset, os.path.join(rel_dir, 'dl', asset_name))
data = open(asset, 'rb').read()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=rel_dir, **k)
    def log_message(self, *a):
        pass

srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{srv.server_port}'
with open(os.path.join(rel_dir, 'latest'), 'w') as f:
    json.dump({'tag_name': 'v' + expected, 'body': 'test release', 'html_url': base,
               'assets': [{'name': asset_name, 'size': len(data),
                           'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
                           'browser_download_url': f'{base}/dl/{asset_name}'}]}, f)

# the old program lives in its own folder (it replaces itself there)
app_dir = os.path.join(work, 'app')
os.makedirs(app_dir)
prog = os.path.join(app_dir, os.path.basename(old_prog))
shutil.copy2(old_prog, prog)
os.chmod(prog, 0o755)

PORT = 5079
env = dict(os.environ, ARTMGR_UPDATE_URL=f'{base}/latest', ARTMGR_DATA_DIR=os.path.join(work, 'data'))
log = open(os.path.join(work, 'old.log'), 'w')
subprocess.Popen([prog, '--port', str(PORT), '--no-browser'], env=env, stdout=log, stderr=subprocess.STDOUT,
                 stdin=subprocess.DEVNULL)


def call(path, body=None, timeout=10):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}',
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def wait(cond, what, secs=90):
    end = time.time() + secs
    while time.time() < end:
        try:
            if cond():
                return
        except Exception:
            pass
        time.sleep(0.5)
    for name in ('old.log', os.path.join('data', 'art-manager.log')):
        p = os.path.join(work, name)
        print(f'--- {name}:\n' + (open(p, errors='replace').read() if os.path.exists(p) else '(missing)'), flush=True)
    try:
        print('--- update state:', call('/api/update', timeout=3), flush=True)
    except Exception as e:
        print('--- update state: no answer', e, flush=True)
    print('--- app folder:', os.listdir(app_dir), flush=True)
    sys.exit(f'FAILED: {what}')

wait(lambda: call('/api/ping')['app'] == 'art-manager', 'old version did not start')
old_version = call('/api/ping')['version']
print('old version running:', old_version, flush=True)
call('/api/config', {'fanart_key': 'keep-me', 'ignored_folders': ['Keep', 'Me'], 'update_auto_install': False})
st = call('/api/update/check', {})
print('check:', {k: st[k] for k in ('current', 'latest', 'available', 'can_self_update', 'writable')}, flush=True)
assert st['available'] and st['can_self_update'] and st['writable'], st
print('install:', call('/api/update/install', {}), flush=True)
wait(lambda: call('/api/ping', timeout=3)['version'] == expected, f'did not restart as {expected}', secs=180)
cfg = call('/api/config')
assert cfg['has_fanart_key'] and cfg['ignored_folders'] == ['Keep', 'Me'], cfg
assert cfg['updated_from'] == old_version, cfg
out = subprocess.run([prog, '--version'], capture_output=True, text=True, timeout=120).stdout
assert expected in out, out
print(f'OK: {old_version} -> {expected} on the same port, settings kept, program file replaced')
try:
    call('/api/quit', {})
except Exception:
    pass
