"""Provider isolation: one failing/slow source never breaks or delays the others."""
import time
import pytest
from art_manager import config, search

def _fake(label, n=2, delay=0, exc=None):
    def fn(*args):
        if delay: time.sleep(delay)
        if exc: raise exc
        return [{'url': f'https://img.invalid/{label}/{i}', 'thumb': '', 'label': f'{label} {i} ' + ' '.join(map(str, args[:-1])),
                 'source': label, 'quality': ''} for i in range(n)]
    return fn

@pytest.fixture
def providers(monkeypatch):
    def install(*provs):
        monkeypatch.setattr(search, 'PROVIDERS', list(provs))
    return install

def _by_key(sources):
    return {s['key']: s for s in sources}

def test_web_provider_raising_does_not_break_others(client, providers):
    P = search.Provider
    providers(P('caa', 'CAA', 'album', _fake('CAA'), phase='slow'),
              P('web', 'Web', 'album', _fake('Web', exc=RuntimeError('DuckDuckGo: rate limited')), phase='slow', web=True))
    r = client.get('/api/search/album/slow', query_string={'artist': 'A', 'album': 'B'}).json
    src = _by_key(r['sources'])
    assert src['caa']['status'] == 'ok' and src['caa']['count'] == 2
    assert src['web']['status'] == 'unavailable' and 'rate limited' in src['web']['message']
    assert [x['source'] for x in r['results']] == ['CAA', 'CAA']

def test_web_provider_timeout_bounded_latency(client, providers):
    P = search.Provider
    providers(P('itunes', 'iTunes', 'artist', _fake('iTunes')),
              P('web', 'Web', 'artist', _fake('Web', delay=5), web=True, timeout=0.5))
    t = time.monotonic()
    r = client.get('/api/search/artist', query_string={'name': 'X'}).json
    assert time.monotonic() - t < 2.0
    src = _by_key(r['sources'])
    assert src['web']['status'] == 'unavailable' and src['web']['message'] == 'timed out'
    assert len(r['results']) == 2

def test_disabled_provider_not_called(client, providers):
    called = []
    P = search.Provider
    providers(P('fanart', 'Fanart.tv', 'artist', lambda *a: called.append(1) or [], enabled=lambda c: False))
    r = client.get('/api/search/artist', query_string={'name': 'X'}).json
    assert r['sources'][0]['status'] == 'disabled' and not called

def test_web_results_after_official_and_deduped(client, providers):
    P = search.Provider
    dup = lambda *a: [{'url': 'https://img.invalid/same', 'thumb': '', 'label': 'x', 'source': 'Web', 'quality': ''}]
    off = lambda *a: [{'url': 'https://img.invalid/same', 'thumb': '', 'label': 'x', 'source': 'Deezer', 'quality': ''},
                      {'url': 'https://img.invalid/2', 'thumb': '', 'label': 'y', 'source': 'Deezer', 'quality': ''}]
    providers(P('web', 'Web', 'artist', dup, web=True), P('deezer', 'Deezer', 'artist', off))
    r = client.get('/api/search/artist', query_string={'name': 'X'}).json
    assert [x['source'] for x in r['results']] == ['Deezer', 'Deezer']

def test_web_search_setting_toggles_web_provider(client):
    client.post('/api/config', json={'web_search': False})
    web = [p for p in search.PROVIDERS if p.web]
    assert web and not any(p.enabled(config.cfg) for p in web)
    client.post('/api/config', json={'web_search': True})
    assert all(p.enabled(config.cfg) for p in web)

def test_web_images_one_engine_enough(monkeypatch):
    def fake(query, page, backend):
        if backend == 'duckduckgo':
            raise RuntimeError('202 challenge')
        return [{'image': 'https://img.invalid/b.jpg', 'thumbnail': 't', 'title': 'Band Album', 'url': 'p', 'width': 1200, 'height': 1000}]
    monkeypatch.setattr(search, '_ddgs_images', fake)
    out = search._web_images('q')
    assert out == [{'url': 'https://img.invalid/b.jpg', 'thumb': 't', 'label': 'Band Album', 'page_url': 'p',
                    'source': 'Bing', 'quality': '1200px'}]
    monkeypatch.setattr(search, '_ddgs_images', lambda *a: (_ for _ in ()).throw(RuntimeError('down')))
    with pytest.raises(RuntimeError):
        search._web_images('q')

def test_discogs_needs_token(client):
    disc = next(p for p in search.PROVIDERS if p.key == 'discogs')
    assert not disc.enabled(config.cfg)
    client.post('/api/config', json={'discogs_token': 'tok'})
    assert disc.enabled(config.cfg)
    assert 'discogs_token' not in client.get('/api/config').json
