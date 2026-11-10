"""Runs tests/ui/ui_tests.js (Playwright, Node) against a live server on a fixture library."""
import os, shutil, signal, socket, subprocess, sys, time, urllib.request
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

def _have_node_playwright():
    if not shutil.which('node'):
        return False
    return subprocess.run(['node', '-e', "require('playwright')"], capture_output=True).returncode == 0

pytestmark = pytest.mark.skipif(not _have_node_playwright(), reason='node + playwright not available')

def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]

@pytest.fixture
def server(make_library, tmp_path):
    root = make_library()
    data = tmp_path / 'data'; data.mkdir()
    port = _free_port()
    env = dict(os.environ, ARTMGR_DATA_DIR=str(data))
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'app.py'), str(root), '--port', str(port)],
                           cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + '/api/scan', timeout=5).read(); break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError('server did not start')
        yield base
    finally:
        proc.terminate(); proc.wait(timeout=5)

def test_browser(server, tmp_path):
    from conftest import PNG_1PX
    png = tmp_path / 'upload.png'
    png.write_bytes(PNG_1PX)
    # Own process group: on a timeout, kill node AND the Chrome it started.
    # (subprocess.run's timeout only kills node; Chrome keeps the output pipe
    # open and the test would then hang forever instead of failing.)
    proc = subprocess.Popen(['node', os.path.join(HERE, 'ui', 'ui_tests.js'), server, str(png)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=(os.name != 'nt'))
    try:
        out, _ = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        out, _ = proc.communicate()
        out += '\nTIMED OUT after 300 s'
    print(out)
    assert proc.returncode == 0, out
