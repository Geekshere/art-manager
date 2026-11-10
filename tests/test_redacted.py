"""Redacted image source, with the API mocked (no network, a fake clock for the rate limit)."""
import json
from urllib.parse import parse_qs, urlsplit
import pytest
from art_manager import config, redacted, search, server
from conftest import PNG_1PX

class FakeResp:
    def __init__(self, status=200, data=None, content=b'', headers=None):
        self.status_code, self._data, self.content = status, data, content
        self.headers = headers or {'Content-Type': 'image/png'}
    def json(self): return self._data
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f'HTTP {self.status_code}')

@pytest.fixture
def api(monkeypatch, client):
    """Fake redacted.sh. Records every request; a fake clock drives the rate limiter."""
    clock = {'t': 1000.0}
    monkeypatch.setattr(redacted.time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(redacted.time, 'sleep', lambda s: clock.__setitem__('t', clock['t'] + s))
    monkeypatch.setattr(redacted.time, 'time', lambda: clock['t'] + 1.7e9)
    monkeypatch.setattr(redacted, '_sent', redacted.collections.deque())
    monkeypatch.setattr(redacted, '_blocked_until', 0.0)
    monkeypatch.setattr(redacted, '_imgauth', None)
    monkeypatch.setattr(redacted, '_img_cache', redacted.collections.OrderedDict())
    calls, script = [], {}
    def get(url, params=None, headers=None, timeout=None):
        calls.append({'url': url, 'params': dict(params or {}), 'headers': dict(headers or {}), 't': clock['t']})
        action = (params or {}).get('action')
        handler = script.get(action or 'image')
        return handler(params) if handler else FakeResp(404)
    monkeypatch.setattr(redacted.requests, 'get', get)
    config.cfg['redacted_key'] = 'KEY123'
    # only the Redacted providers: no real network calls to the other sources
    monkeypatch.setattr(search, 'PROVIDERS', [p for p in search.PROVIDERS if p.key == 'redacted'])
    script['browse'] = lambda p: FakeResp(data={'status': 'success', 'response': {'results': [
        {'groupId': 1, 'groupName': 'Dummy', 'artist': 'Portishead', 'groupYear': 1994, 'releaseType': 'Album',
         'cover': 'https://ptpimg.me/abc123.jpg'},
        {'groupId': 2, 'groupName': 'Dummy (Deluxe)', 'artist': 'Portishead', 'groupYear': 2014, 'releaseType': 'Album'},
    ]}})
    script['torrentgroup'] = lambda p: FakeResp(data={'status': 'success', 'response': {'group': {'wikiImage': 'i/sFEgBaH3qlo.png'}}})
    script['artist'] = lambda p: FakeResp(data={'status': 'success', 'response': {'name': 'Portishead', 'image': '/i/Art1st.jpg'}})
    script['imgauth'] = lambda p: FakeResp(data={'status': 'success', 'response': {'h': 'SECRET-H', 'e': clock['t'] + 1.7e9 + 3600, 'u': 42}})
    script['image'] = lambda p: FakeResp(content=PNG_1PX)
    return {'calls': calls, 'script': script, 'clock': clock}

def test_album_covers_and_group_fallback(api):
    out = redacted.album_covers('Portishead', 'Dummy')
    assert [r['label'] for r in out] == ['Portishead — Dummy (1994) · Album', 'Portishead — Dummy (Deluxe) (2014) · Album']
    assert out[0]['url'] == 'https://ptpimg.me/abc123.jpg'                        # external host: as is
    assert out[1]['url'] == '/api/image/redacted?path=i/sFEgBaH3qlo.png'          # own host: proxied
    browse = api['calls'][0]
    assert browse['params'] == {'action': 'browse', 'groupname': 'Dummy', 'artistname': 'Portishead'}
    assert browse['headers']['Authorization'] == 'KEY123' and 'ArtManager' in browse['headers']['User-Agent']
    assert [c['params']['action'] for c in api['calls']] == ['browse', 'torrentgroup']   # one lookup, only where needed

def test_group_lookups_are_capped(api):
    api['script']['browse'] = lambda p: FakeResp(data={'status': 'success', 'response': {'results': [
        {'groupId': i, 'groupName': f'G{i}', 'artist': 'A'} for i in range(6)]}})
    redacted.album_covers('A', 'G')
    assert sum(c['params'].get('action') == 'torrentgroup' for c in api['calls']) == redacted.MAX_GROUP_LOOKUPS

def test_artist_image(api):
    out = redacted.artist_image('Portishead')
    assert out == [{'url': '/api/image/redacted?path=i/Art1st.jpg', 'thumb': '/api/image/redacted?path=i/Art1st.jpg',
                    'source': 'Redacted', 'quality': '', 'label': 'Portishead'}]

def test_never_more_than_9_requests_in_10_seconds(api):
    for _ in range(25):
        redacted.artist_image('Portishead')
    times = [c['t'] for c in api['calls']]
    assert len(times) == 25
    for i in range(len(times)):
        assert sum(1 for t in times if times[i] <= t < times[i] + 10) <= 9

def test_retry_after_pauses_everything(api):
    api['script']['artist'] = lambda p: FakeResp(429, headers={'Retry-After': '7'})
    with pytest.raises(redacted.RedactedError, match='rate limited'):
        redacted.artist_image('X')
    t429 = api['clock']['t']
    api['script']['artist'] = lambda p: FakeResp(data={'status': 'success', 'response': {'image': ''}})
    redacted.artist_image('X')
    assert api['calls'][-1]['t'] >= t429 + 7

def test_rejected_key_explained(api):
    api['script']['artist'] = lambda p: FakeResp(401)
    with pytest.raises(redacted.RedactedError, match='Torrent scope'):
        redacted.artist_image('X')

def test_disabled_without_key(api):
    config.cfg['redacted_key'] = ''
    assert redacted.album_covers('A', 'B') == [] and api['calls'] == []
    r = server.app.test_client().get('/api/search/artist', query_string={'name': 'x'}).json
    assert next(s for s in r['sources'] if s['key'] == 'redacted')['status'] == 'disabled'

def test_image_proxy_keeps_credentials_on_server(api, client):
    r = client.get('/api/image/redacted', query_string={'path': 'i/sFEgBaH3qlo.png'})
    assert r.status_code == 200 and r.data == PNG_1PX
    img = api['calls'][-1]
    assert img['url'] == 'https://redacted.sh/i/sFEgBaH3qlo.png'
    assert img['params'] == {'h': 'SECRET-H', 'e': img['params']['e'], 'u': 42}
    # credentials appear in no response the browser can see
    for path in ('/api/config', '/api/search/album/slow?artist=Portishead&album=Dummy', '/api/search/artist?name=P'):
        assert 'SECRET-H' not in client.get(path).get_data(as_text=True)
        assert 'KEY123' not in client.get(path).get_data(as_text=True)
    assert client.get('/api/config').json['has_redacted_key'] is True

@pytest.mark.parametrize('bad', ['../etc/passwd', 'i/../../x.png', 'i/abc.exe', 'https://evil.example/i/a.png', 'x/abc.png'])
def test_image_proxy_rejects_other_paths(api, client, bad):
    assert client.get('/api/image/redacted', query_string={'path': bad}).status_code == 400

def test_choosing_a_redacted_cover_embeds_it(api, client, make_library):
    root = make_library()
    lib = client.get('/api/scan', query_string={'root': str(root)}).json
    al = next(a for a in lib['artists'] if a['name'] == 'Alpha')['albums'][1]
    r = client.post('/api/prepare-embed', json={'album_path': al['path'], 'disc_info': al['disc_info'],
                                                'source': 'url', 'image_url': '/api/image/redacted?path=i/sFEgBaH3qlo.png'})
    assert r.status_code == 200, r.json
    out = client.get(f"/api/embed/{r.json['embed_id']}").get_data(as_text=True)
    assert '"errors": []' in out

def test_search_api_shows_redacted_first_in_slow_phase(api, client):
    r = client.get('/api/search/album/slow', query_string={'artist': 'Portishead', 'album': 'Dummy'}).json
    src = {s['key']: s for s in r['sources']}
    assert src['redacted']['status'] == 'ok' and src['redacted']['count'] == 2
    assert r['results'][0]['source'] == 'Redacted'
