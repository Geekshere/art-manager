"""Live smoke test against the real search services. Skipped unless ARTMGR_LIVE=1
(CI and offline runs skip it). Run: ARTMGR_LIVE=1 pytest -q tests/test_live_search.py -s"""
import os
import pytest
from art_manager import search

pytestmark = pytest.mark.skipif(os.environ.get('ARTMGR_LIVE') != '1', reason='set ARTMGR_LIVE=1 to hit real services')

def test_live_web_images():
    res = search._web_images('Radiohead OK Computer album cover')
    print({r['source'] for r in res}, len(res))
    assert res and all(r['url'].startswith('http') for r in res)

def test_live_album_and_artist_search():
    for kind, phase, args in [('album', 'fast', ('Radiohead', 'OK Computer')),
                              ('album', 'slow', ('Radiohead', 'OK Computer')),
                              ('artist', None, ('Radiohead',))]:
        res, src = search.run_providers(kind, phase, args, 0)
        print(kind, phase, [(s['label'], s['status'], s['count'], s.get('message', '')) for s in src])
        assert res, f'no results at all for {kind}/{phase}'
