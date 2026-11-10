"""
MusicBrainz lookups for the tag editor.

Rules (musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting): at most one
request per second, with a User-Agent that identifies the application. Field
names written follow MusicBrainz Picard's Vorbis mapping, so files tagged
here look the same as files tagged with Picard.
"""
import re, threading, time

import requests

from . import __version__

API = 'https://musicbrainz.org/ws/2/'
UA = f'ArtManager/{__version__} ( https://github.com/Geekshere/art-manager )'

_lock = threading.Lock()
_last = [0.0]


def _get(path, **params):
    with _lock:                                   # one request per second, app-wide
        wait = _last[0] + 1.05 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
    r = requests.get(API + path, params={'fmt': 'json', **params},
                     headers={'User-Agent': UA, 'Accept': 'application/json'}, timeout=20)
    if r.status_code == 503:
        raise RuntimeError('MusicBrainz is busy (rate limited) — try again in a moment')
    r.raise_for_status()
    return r.json()

def _q(s):
    """Quote a value for a Lucene query (escape backslashes and double quotes)."""
    return '"' + s.strip().replace('\\', '\\\\').replace('"', '\\"') + '"'

def _credit(ac):
    return ''.join(c.get('name', '') + c.get('joinphrase', '') for c in ac or [])

def _credit_sort(ac):
    return ''.join(c.get('artist', {}).get('sort-name', c.get('name', '')) + c.get('joinphrase', '') for c in ac or [])

def _credit_ids(ac):
    return [c['artist']['id'] for c in ac or [] if c.get('artist', {}).get('id')]


def search_releases(artist, album, track_count=None, hint=''):
    """
    Candidate releases (editions) for an album, best matches first: same
    track count as the files, then agreement with the edition hint from the
    folder name ('2013 Remaster' prefers releases from 2013 / mentioning
    "remaster"), then MusicBrainz's own score.
    """
    q = f'release:{_q(album)}'
    if artist:
        q += f' AND artist:{_q(artist)}'
    data = _get('release/', query=q, limit=15)
    out = []
    for r in data.get('releases', []):
        media = r.get('media', [])
        tracks = sum(m.get('track-count', 0) for m in media)
        li = r.get('label-info') or []
        out.append({
            'id': r['id'], 'title': r.get('title', ''), 'artist': _credit(r.get('artist-credit')),
            'date': r.get('date', ''), 'country': r.get('country', ''),
            'label': ', '.join(dict.fromkeys(x['label']['name'] for x in li if x.get('label'))),
            'catno': ', '.join(dict.fromkeys(x['catalog-number'] for x in li if x.get('catalog-number'))),
            'barcode': r.get('barcode') or '', 'disambiguation': r.get('disambiguation', ''),
            'format': ' + '.join(f"{m.get('format', '?')}" for m in media),
            'discs': len(media), 'tracks': tracks, 'status': r.get('status', ''),
            'type': (r.get('release-group') or {}).get('primary-type', ''),
            'score': r.get('score', 0),
            'track_match': track_count is None or tracks == track_count,
        })
    words = {w.casefold() for w in re.findall(r'\w+', hint or '') if len(w) > 2}
    for x in out:
        text = f"{x['date']} {x['disambiguation']} {x['format']} {x['country']}".casefold()
        x['hint_match'] = sum(1 for w in words if w in text)
    out.sort(key=lambda x: (not x['track_match'], -x['hint_match'], -x['score']))
    return out


def release_tags(mbid):
    """
    -> {'album': {FIELD: [values]}, 'tracks': [{'disc': n, 'position': n, 'tags': {...}, 'length': s}],
        'summary': {...}} for one release.
    """
    r = _get(f'release/{mbid}', inc='recordings artist-credits labels release-groups media isrcs')
    rg = r.get('release-group') or {}
    li = r.get('label-info') or []
    media = r.get('media', [])
    album = {
        'ALBUM': [r.get('title', '')],
        'ALBUMARTIST': [_credit(r.get('artist-credit'))],
        'ALBUMARTISTSORT': [_credit_sort(r.get('artist-credit'))],
        'DATE': [r.get('date', '')],
        'ORIGINALDATE': [rg.get('first-release-date', '')],
        'RELEASECOUNTRY': [r.get('country') or ''],
        'RELEASESTATUS': [(r.get('status') or '').lower()],
        'RELEASETYPE': [x.lower() for x in [rg.get('primary-type')] + (rg.get('secondary-types') or []) if x],
        'LABEL': list(dict.fromkeys(x['label']['name'] for x in li if x.get('label'))),
        'CATALOGNUMBER': list(dict.fromkeys(x['catalog-number'] for x in li if x.get('catalog-number'))),
        'BARCODE': [r.get('barcode') or ''],
        'MEDIA': [media[0].get('format', '')] if media else [],
        'DISCTOTAL': [str(len(media))],
        'MUSICBRAINZ_ALBUMID': [r['id']],
        'MUSICBRAINZ_RELEASEGROUPID': [rg.get('id', '')],
        'MUSICBRAINZ_ALBUMARTISTID': _credit_ids(r.get('artist-credit')),
        'MUSICBRAINZ_ALBUMCOMMENT': [r.get('disambiguation', '')],
        'SCRIPT': [(r.get('text-representation') or {}).get('script') or ''],
        'LANGUAGE': [(r.get('text-representation') or {}).get('language') or ''],
    }
    album = {k: [v for v in vals if v] for k, vals in album.items()}
    tracks = []
    for m in media:
        for t in m.get('tracks', []):
            rec = t.get('recording') or {}
            ac = t.get('artist-credit') or rec.get('artist-credit')
            tags = {
                'TITLE': [t.get('title') or rec.get('title', '')],
                'ARTIST': [_credit(ac)],
                'ARTISTSORT': [_credit_sort(ac)],
                'TRACKNUMBER': [str(t.get('position') or t.get('number', ''))],
                'TRACKTOTAL': [str(m.get('track-count', ''))],
                'DISCNUMBER': [str(m.get('position', 1))],
                'DISCSUBTITLE': [m.get('title', '')],
                'MUSICBRAINZ_TRACKID': [rec.get('id', '')],
                'MUSICBRAINZ_RELEASETRACKID': [t.get('id', '')],
                'MUSICBRAINZ_ARTISTID': _credit_ids(ac),
                'ISRC': rec.get('isrcs') or [],
            }
            tracks.append({'disc': m.get('position', 1), 'position': t.get('position', 0),
                           'length': round((t.get('length') or rec.get('length') or 0) / 1000),
                           'tags': {k: [v for v in vals if v] for k, vals in tags.items()}})
    summary = {'title': r.get('title', ''), 'artist': _credit(r.get('artist-credit')), 'date': r.get('date', ''),
               'disambiguation': r.get('disambiguation', ''), 'country': r.get('country', ''),
               'url': f'https://musicbrainz.org/release/{r["id"]}'}
    return {'album': album, 'tracks': tracks, 'summary': summary, 'edition': r.get('disambiguation', '')}
