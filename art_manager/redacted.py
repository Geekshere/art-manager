"""
Redacted (redacted.sh) as an image source — album covers from release groups
and artist images — using the user's own API key.

Rules from Redacted's API documentation, followed strictly:
- Authentication: header `Authorization: <api key>`. A key with only the
  "Torrent" scope is enough (browse needs no scope; artist / torrentgroup /
  imgauth are torrent-scope).
- Rate limit with an API key: at most 10 requests per 10 seconds per user.
  We allow 9 per 10 s across the whole app and wait rather than exceed it;
  a 429 response's Retry-After pauses every request until it has passed.
- Pictures on Redacted's own image host need short-lived credentials from
  `imgauth`, which must be treated like a password. They never leave this
  server: such images are served to the browser through
  /api/image/redacted?path=i/<id>.<ext>, and fetched here with the
  credentials added.
"""
import collections, re, threading, time
from urllib.parse import quote

import requests

from . import __version__
from .config import cfg

BASE = 'https://redacted.sh/'
UA = f'ArtManager/{__version__} (+https://github.com/Geekshere/art-manager)'
WINDOW, MAX_IN_WINDOW = 10.0, 9
MAX_GROUP_LOOKUPS = 3
IMAGE_PATH = re.compile(r'^[it]/[A-Za-z0-9_-]+\.(?:jpe?g|png|gif|webp)$', re.I)

_lock = threading.Lock()
_sent = collections.deque()      # times of recent requests
_blocked_until = 0.0             # from a 429's Retry-After
_imgauth = None                  # {'h', 'e', 'u'}
_img_cache = collections.OrderedDict()


class RedactedError(RuntimeError):
    pass


def enabled():
    return bool(cfg.get('redacted_key'))

def _wait_turn():
    """Block until a request is allowed by the rate limit (never more than 9 per 10 s)."""
    while True:
        with _lock:
            now = time.monotonic()
            if now < _blocked_until:
                wait = _blocked_until - now
            else:
                while _sent and now - _sent[0] >= WINDOW:
                    _sent.popleft()
                if len(_sent) < MAX_IN_WINDOW:
                    _sent.append(now)
                    return
                wait = WINDOW - (now - _sent[0]) + 0.05
        if wait > 20:
            raise RedactedError('rate limited — try again in a moment')
        time.sleep(wait)

def _get(action, timeout=15, **params):
    global _blocked_until
    key = cfg.get('redacted_key')
    if not key:
        raise RedactedError('no API key')
    _wait_turn()
    r = requests.get(BASE + 'ajax.php', params={'action': action, **params},
                     headers={'Authorization': key, 'User-Agent': UA}, timeout=timeout)
    if r.status_code == 429:
        try: retry = float(r.headers.get('Retry-After', '10'))
        except ValueError: retry = 10.0
        with _lock:
            _blocked_until = time.monotonic() + retry
        raise RedactedError('rate limited by Redacted')
    if r.status_code in (401, 403):
        raise RedactedError('API key rejected (check the key and that it has the Torrent scope)')
    r.raise_for_status()
    data = r.json()
    if data.get('status') != 'success':
        raise RedactedError(str(data.get('error') or 'request failed'))
    return data.get('response')


# ── images ────────────────────────────────────────────────────────────────────

def _site_image_path(url):
    """'i/abc.png', '/i/abc.png' or 'https://redacted.sh/i/abc.png' -> 'i/abc.png'; external URLs -> None."""
    if not url:
        return None
    u = url.strip()
    for prefix in ('https://redacted.sh/', 'http://redacted.sh/', '/'):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    u = u.split('?', 1)[0]
    return u if IMAGE_PATH.match(u) else None

def image_result(url):
    """(full, thumb) URLs the browser can load for an image reference from the API, or None."""
    if not url:
        return None
    path = _site_image_path(url)
    if path:
        proxied = '/api/image/redacted?path=' + quote(path)
        return proxied, proxied
    if url.startswith(('http://', 'https://')):
        return url, url
    return None

def _credentials():
    global _imgauth
    with _lock:
        cred = _imgauth
    if cred and cred.get('e', 0) - 60 > time.time():
        return cred
    cred = _get('imgauth')
    with _lock:
        _imgauth = cred
    return cred

def fetch_site_image(path):
    """Bytes + content type of an image on Redacted's image host (credentials added here)."""
    if not IMAGE_PATH.match(path or ''):
        raise RedactedError('not a Redacted image path')
    with _lock:
        if path in _img_cache:
            _img_cache.move_to_end(path)
            return _img_cache[path]
    cred = _credentials()
    r = requests.get(BASE + path, params={'h': cred['h'], 'e': cred['e'], 'u': cred['u']},
                     headers={'User-Agent': UA}, timeout=20)
    r.raise_for_status()
    item = (r.content, r.headers.get('Content-Type', 'image/jpeg').split(';')[0])
    with _lock:
        _img_cache[path] = item
        while len(_img_cache) > 40:
            _img_cache.popitem(last=False)
    return item


# ── search providers ──────────────────────────────────────────────────────────

def album_covers(artist, album, page=0):
    if page or not enabled():
        return []
    params = {'groupname': album}
    if artist:
        params['artistname'] = artist
    res = (_get('browse', **params) or {}).get('results', [])[:6]
    out, lookups = [], 0
    for g in res:
        img = g.get('cover') or g.get('wikiImage')
        if not img and lookups < MAX_GROUP_LOOKUPS and g.get('groupId'):
            lookups += 1
            try:
                img = ((_get('torrentgroup', id=g['groupId']) or {}).get('group') or {}).get('wikiImage')
            except RedactedError:
                img = None
        urls = image_result(img)
        if not urls:
            continue
        year = f" ({g['groupYear']})" if g.get('groupYear') else ''
        kind = f" · {g['releaseType']}" if isinstance(g.get('releaseType'), str) else ''
        out.append({'url': urls[0], 'thumb': urls[1], 'source': 'Redacted', 'quality': '',
                    'label': f"{g.get('artist', artist)} — {g.get('groupName', '')}{year}{kind}"})
    return out

def artist_image(name, page=0):
    if page or not enabled():
        return []
    a = _get('artist', artistname=name) or {}
    urls = image_result(a.get('image'))
    if not urls:
        return []
    return [{'url': urls[0], 'thumb': urls[1], 'source': 'Redacted', 'quality': '',
             'label': a.get('name', name)}]
