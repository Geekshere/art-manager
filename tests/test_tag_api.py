"""Tag editor API: read, look up (mocked MusicBrainz / Redacted), pair tracks, apply, undo, bulk cleanup."""
import time
import pytest
from art_manager import config, musicbrainz, redacted, server, tags
from conftest import add_flac

@pytest.fixture
def album(client, tmp_path, flac_bytes, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path / 'data'))
    root = tmp_path / 'Music'
    a = root / 'Portishead' / 'Dummy (2013 Remaster) [FLAC 24-96]'
    for i, (t, extra) in enumerate([('Mysterons', [('album_artist', 'Portishead'), ('ALBUMARTIST', 'Portishead')]),
                                    ('Sour Times', [('ALBUM ARTIST', 'Portishead')])], 1):
        p = a / f'{i:02d} - {t}.flac'
        add_flac(p, flac_bytes)
        tags.write(str(p), [('TITLE', t), ('tracknumber', str(i)), ('ALBUM', 'Dummy')] + extra)
    add_flac(root / 'Portishead' / 'Dummy' / '01.flac', flac_bytes)          # another edition
    lib = client.get('/api/scan', query_string={'root': str(root)}).json
    al = next(x for x in lib['artists'][0]['albums'] if x['name'].startswith('Dummy (2013'))
    return {'name': al['name'], 'path': al['path'], 'disc_info': al['disc_info']}

def test_read_reports_problems_hint_and_siblings(client, album):
    d = client.post('/api/tags/read', json=album).json
    assert [t['name'] for t in d['tracks']] == ['01 - Mysterons.flac', '02 - Sour Times.flac']
    t1 = d['tracks'][0]
    assert t1['tags']['ALBUMARTIST'] == ['Portishead'] and t1['tags']['TRACKNUMBER'] == ['1']
    assert any(p['code'] == 'tag_spellings' for p in t1['problems'])
    assert d['edition_hint'] == '2013 Remaster'
    assert [s['name'] for s in d['siblings']] == ['Dummy']

def test_map_tracks():
    remote = [{'disc': 1, 'position': 1}, {'disc': 1, 'position': 2}, {'disc': 2, 'position': 1}]
    by_tags = [{'folder': 0, 'tags': {'DISCNUMBER': ['2'], 'TRACKNUMBER': ['1']}},
               {'folder': 0, 'tags': {'TRACKNUMBER': ['1']}}]
    assert server.map_tracks(by_tags, remote) == ([2, 0], 'by the track and disc numbers in your files')
    folders = [{'folder': 1, 'tags': {}}, {'folder': 1, 'tags': {}}, {'folder': 2, 'tags': {}}]
    assert server.map_tracks(folders, remote)[0] == [0, 1, 2]
    assert server.map_tracks([{'folder': 0, 'tags': {}}] * 4, remote)[0] == [0, 1, 2, None]

def test_musicbrainz_proposals_apply_and_undo(client, album, monkeypatch):
    monkeypatch.setattr(musicbrainz, 'search_releases', lambda *a, **k: [{'id': 'mbid-1', 'title': 'Dummy'}])
    monkeypatch.setattr(musicbrainz, 'release_tags', lambda mbid: {
        'album': {'ALBUM': ['Dummy'], 'ALBUMARTIST': ['Portishead'], 'DATE': ['1994-08-22'], 'MUSICBRAINZ_ALBUMID': [mbid]},
        'tracks': [{'disc': 1, 'position': 1, 'length': 306, 'tags': {'TITLE': ['Mysterons'], 'TRACKNUMBER': ['1']}},
                   {'disc': 1, 'position': 2, 'length': 251, 'tags': {'TITLE': ['Sour Times'], 'TRACKNUMBER': ['2']}}],
        'summary': {'title': 'Dummy', 'url': 'https://musicbrainz.org/release/x'}, 'edition': ''})
    assert client.post('/api/tags/lookup', json={'source': 'musicbrainz', 'album': 'Dummy'}).json['results'][0]['id'] == 'mbid-1'
    rel = client.post('/api/tags/release', json=dict(album, source='musicbrainz', id='mbid-1')).json
    assert rel['how'].startswith('by the track') and all(p['matched'] for p in rel['proposals'])
    before = [tags.read_raw(p['path']) for p in rel['proposals']]
    changes = [{'path': p['path'], 'tags': dict(p['tags'], MUSICBRAINZ_ALBUMCOMMENT=['2013 Remaster'])} for p in rel['proposals']]
    r = client.post('/api/tags/apply', json={'changes': changes, 'label': 'Tags from MusicBrainz'}).json
    assert r['errors'] == [] and r['written'] == 2
    after = tags.read_raw(rel['proposals'][1]['path'])
    assert ('ALBUMARTIST', 'Portishead') in after and not any(k == 'ALBUM ARTIST' for k, _ in after)
    assert ('MUSICBRAINZ_ALBUMCOMMENT', '2013 Remaster') in after and ('DATE', '1994-08-22') in after
    backups = client.post('/api/tags/read', json=album).json['backups']
    assert backups[0]['id'] == r['backup']
    client.post('/api/tags/undo', json={'id': r['backup']})
    assert [tags.read_raw(p['path']) for p in rel['proposals']] == before

def test_apply_confined_and_raw_validated(client, album, tmp_path):
    assert client.post('/api/tags/apply', json={'changes': [{'path': str(tmp_path / 'x.flac'), 'tags': {}}]}).status_code == 403
    path = album['path'] + '/01 - Mysterons.flac'
    assert client.post('/api/tags/apply-raw', json={'path': path, 'pairs': [['BAD=KEY', 'x']]}).status_code == 400
    r = client.post('/api/tags/apply-raw', json={'path': path, 'pairs': [['title', 'Raw'], ['Custom Field', 'v']]}).json
    assert r['errors'] == [] and tags.read_raw(path) == [('title', 'Raw'), ('Custom Field', 'v')]

def test_bulk_cleanup_job(client, album):
    job = client.post('/api/tags/cleanup', json={'albums': [dict(album, label='Dummy')]}).json
    for _ in range(100):
        d = client.get(f"/api/tags/cleanup/{job['id']}").json
        if d['status'] != 'running': break
        time.sleep(0.05)
    assert d['files'] == 2 and d['errors'] == [] and len(d['backups']) == 1
    for name in ('01 - Mysterons.flac', '02 - Sour Times.flac'):
        raw = tags.read_raw(album['path'] + '/' + name)
        assert sum(1 for k, _ in raw if tags.canonical(k) == 'ALBUMARTIST') == 1
        assert all(k == k.upper() for k, _ in raw)

def test_redacted_edition_matching(client, album, monkeypatch):
    config.cfg['redacted_key'] = 'k'
    monkeypatch.setattr(redacted, '_get', lambda action, **p: {
        'group': {'name': 'Dummy', 'year': 1994, 'recordLabel': 'Go! Beat', 'catalogueNumber': '828 522-2', 'releaseType': 1,
                  'musicInfo': {'artists': [{'name': 'Portishead'}]}},
        'torrents': [
            {'media': 'CD', 'format': 'FLAC', 'encoding': 'Lossless', 'remasterTitle': '', 'remasterYear': 0,
             'fileList': '01 Mysterons.flac{{{1}}}|||02 Sour Times.flac{{{2}}}'},
            {'media': 'WEB', 'format': 'FLAC', 'encoding': '24bit Lossless', 'remasterTitle': 'Remaster', 'remasterYear': 2013,
             'remasterRecordLabel': 'Universal', 'remasterCatalogueNumber': 'X123',
             'fileList': '01 - Mysterons.flac{{{1}}}|||02 - Sour Times.flac{{{2}}}|||cover.jpg{{{3}}}'}]})
    d = client.post('/api/tags/release', json=dict(album, source='redacted', id=5)).json
    best = d['editions'][0]
    assert best['match'] == 1.0 and best['edition'] == '2013 Remaster'
    assert best['tags']['LABEL'] == ['Universal'] and best['tags']['DATE'] == ['2013']
    assert best['tags']['ORIGINALDATE'] == ['1994'] and best['tags']['RELEASETYPE'] == ['album']

def test_new_file_checks(client, tmp_path, flac_bytes):
    from art_manager import check
    root = tmp_path / 'M' / 'A' / 'Al'
    for i in (1, 2):
        add_flac(root / f'0{i}.flac', flac_bytes)
        tags.write(str(root / f'0{i}.flac'), [('TITLE', 'CafÃ©'), ('ALBUM', 'Al'), ('ARTIST', 'A'), ('TRACKNUMBER', str(i)),
                                               ('ALBUMARTIST', 'A' if i == 1 else 'B'), ('album_artist', 'A')])
    (root / '._01.flac').write_bytes(b'junk'); (root / 'Thumbs.db').write_bytes(b'x'); (root / '03.tak').write_bytes(b'x')
    res = check.check_album({'name': 'Al', 'path': str(root), 'disc_info': [{'path': str(root)}]})
    codes = {i['code'] for i in res['issues']} | {i['code'] for f in res['files'] for i in f['issues']}
    assert {'mojibake', 'tag_spellings', 'albumartist_mismatch', 'junk_files', 'other_audio'} <= codes
    assert len([f for f in res['files'] if f['kind'] == 'track']) == 2      # ._01.flac is not a track

def test_musicbrainz_ranking_prefers_edition_hint(monkeypatch):
    rel = lambda i, date, dis='': {'id': str(i), 'title': 'Dummy', 'date': date, 'disambiguation': dis, 'score': 100 - i,
                                   'media': [{'format': 'CD', 'track-count': 11}], 'artist-credit': []}
    monkeypatch.setattr(musicbrainz, '_get', lambda path, **p: {'releases': [
        rel(0, '1994'), rel(1, '2013-09-02', 'remaster'), rel(2, '2013'), rel(3, '1994', 'promo')]})
    ids = [r['id'] for r in musicbrainz.search_releases('Portishead', 'Dummy', 11, '2013 Remaster')]
    assert ids[:2] == ['1', '2']            # both 2013 first, the one also saying "remaster" on top
