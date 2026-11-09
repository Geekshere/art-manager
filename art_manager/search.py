"""
Image search sources and the provider registry.

Each source is a plain function returning [{url, thumb, label, source, quality}]
that RAISES on failure; run_providers() turns an exception or timeout into an
"unavailable" status for that one source and never lets it break the others.
"""
import concurrent.futures, re, time

import requests

from .config import cfg

_MB_UA = 'ArtManager/0.3 (+https://github.com/Geekshere/art-manager)'

# ── Size helpers ──────────────────────────────────────────────────────────────

def _size_from_itunes_url(url):
    """Parse size from iTunes CDN URLs: 3000x3000bb → '3000px'."""
    m = re.search(r'(\d+)x(\d+)bb', url)
    return f"{max(int(m.group(1)), int(m.group(2)))}px" if m else '3000px'

def _size_from_url(url):
    """Try to extract size from common CDN URL patterns."""
    for pat in [r'[_/](\d{3,5})x(\d{3,5})[_/.]', r'/(\d{3,5})/(\d{3,5})/',
                r'-(\d{4,5})x(\d{4,5})\.']:
        m = re.search(pat, url)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if 200 <= w <= 10000 and 200 <= h <= 10000:
                return f"{max(w, h)}px"
    return None

def _clean_artist(s):
    """
    Clean artist name for search APIs.
    Folder names like '-Some-Band-' → 'Some Band'
    MusicBrainz Lucene treats '-' as NOT, ':' as field separator, etc.
    """
    s = s.strip()
    # Remove leading/trailing punctuation that folders often add
    s = s.strip('-_.')
    # Replace special MB Lucene operators with spaces
    s = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', ' ', s)
    return ' '.join(s.split())   # collapse whitespace

def _clean_album(s):
    """
    Clean album name for search APIs.
    'Album Title (1984)' → 'Album Title'  (year suffixes confuse MusicBrainz)
    """
    s = s.strip()
    # Remove trailing year like " (1984)" or " [1984]"
    s = re.sub(r'\s*[\(\[]\d{4}[\)\]]\s*$', '', s)
    s = re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', ' ', s)
    return ' '.join(s.split())

# ── Search sources ────────────────────────────────────────────────────────────
#
# Each source is a plain function that returns a list of result dicts
#   {url, thumb, label, source, quality}
# and RAISES on failure (network error, bad response...). The aggregator below
# turns an exception or a timeout into an "unavailable" status for that one
# source; it never breaks the others.

def _dedup(results):
    seen, out = set(), []
    for r in results:
        u = r.get('url', '')
        if u and u not in seen:
            seen.add(u); out.append(r)
    return out

def _filter_by_relevance(results, artist, album):
    """
    Filter a result list to entries that mention at least one keyword from
    the artist or album name. Used on general web image results to drop
    stock photos, random pages, etc. Sorts survivors by match score.

    Uses 4+ character words to avoid false positives from short common words.
    """
    keywords = set()
    for s in [_clean_artist(artist), _clean_album(album)]:
        keywords.update(w.lower() for w in s.split() if len(w) >= 4)
    if not keywords:
        return results   # nothing to filter on

    scored = []
    for r in results:
        text = (r.get('label', '') + ' ' + r.get('url', '') + ' ' + r.get('page_url', '')).lower()
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored]


def _deezer_real_image(url):
    """
    Deezer real images have a long MD5 hash in the path:
      /images/artist/a1b2c3d4e5f6.../size
    Placeholders have an empty or very short segment (double-slash):
      /images/artist//size
    """
    if not url: return False
    if 'default' in url.lower(): return False
    m = re.search(r'/images/(?:artist|cover)/([^/]*)', url)
    if m:
        return len(m.group(1)) >= 16   # real hashes are 32 chars
    return True

def _wiki_thumb_to_orig(thumb_url):
    """Wikipedia CDN thumb → original full-res image."""
    m = re.match(
        r'(https://upload\.wikimedia\.org/wikipedia/[^/]+)/thumb/(.+)/\d+px-[^/]+$',
        thumb_url)
    return f"{m.group(1)}/{m.group(2)}" if m else thumb_url

def _get_json(url, **kw):
    r = requests.get(url, **kw)
    r.raise_for_status()
    return r.json()

def _itunes_album(artist, album, page):
    data = _get_json('https://itunes.apple.com/search',
        params={'term': f'{artist} {album}', 'entity': 'album',
                'limit': 6, 'offset': page * 6}, timeout=10)
    out = []
    for a in data.get('results', []):
        u = a.get('artworkUrl100', '')
        if u:
            full = re.sub(r'\d+x\d+bb', '3000x3000bb', u)
            out.append({
                'url': full, 'thumb': re.sub(r'\d+x\d+bb', '300x300bb', u),
                'label': f"{a.get('artistName','')} — {a.get('collectionName','')}",
                'source': 'iTunes', 'quality': _size_from_itunes_url(full),
            })
    return out

def _deezer_album(artist, album, page):
    data = _get_json('https://api.deezer.com/search/album',
        params={'q': f'{artist} {album}', 'index': page * 6, 'limit': 6}, timeout=10)
    out = []
    for a in data.get('data', []):
        cov = a.get('cover_xl') or a.get('cover_big') or a.get('cover', '')
        if cov and _deezer_real_image(cov):
            out.append({
                'url': cov, 'thumb': a.get('cover_medium', cov),
                'label': f"{a.get('artist',{}).get('name','')} — {a.get('title','')}",
                'source': 'Deezer', 'quality': _size_from_url(cov) or '1000px',
            })
    return out

def _wikipedia_artist(name, page=0):
    """
    Artist image from Wikipedia — no API key, free, press-photo quality.
    Returns original full-res image when possible.
    """
    if page:
        return []   # one image per article; nothing more to page through
    data = _get_json('https://en.wikipedia.org/w/api.php', params={
        'action': 'query', 'prop': 'pageimages',
        'titles': name, 'format': 'json',
        'pithumbsize': 1200, 'redirects': 1,
    }, headers={'User-Agent': _MB_UA}, timeout=10)
    pages = data.get('query', {}).get('pages', {})
    out = []
    for pg in pages.values():
        if 'missing' in pg: continue
        thumb = pg.get('thumbnail', {})
        src   = thumb.get('source', '')
        if not src: continue
        orig = _wiki_thumb_to_orig(src)
        w    = thumb.get('width', 0)
        sz   = _size_from_url(orig) or (f'{w}px' if w > 0 else '1000px')
        out.append({'url': orig, 'thumb': src,
                    'label': pg.get('title', name),
                    'source': 'Wikipedia', 'quality': sz})
    return out

def _discogs_album(artist, album, page=0):
    """
    Album art from Discogs — community high-res scans.
    Especially strong for vinyl/physical releases and non-English music.
    Discogs only includes image URLs for authenticated requests, so this
    needs a (free) personal access token: discogs.com → Settings → Developers.
    """
    token = cfg.get('discogs_token', '')
    if page or not token:
        return []
    a_clean  = _clean_artist(artist)
    al_clean = _clean_album(album)
    q = f'{a_clean} {al_clean}'.strip() or f'{artist} {album}'
    data = _get_json('https://api.discogs.com/database/search',
        params={'q': q, 'type': 'master', 'per_page': 8},
        headers={'User-Agent': _MB_UA, 'Authorization': f'Discogs token={token}'}, timeout=12)
    out = []
    for item in data.get('results', []):
        cov = item.get('cover_image', '')
        if (cov and 'spacer' not in cov
                and not cov.endswith('.gif')
                and 'blank' not in cov.lower()):
            out.append({
                'url':   cov,
                'thumb': item.get('thumb', cov),
                'label': item.get('title', ''),
                'source': 'Discogs',
                'quality': _size_from_url(cov) or 'scan',
            })
    return out

def _caa_album(artist, album, page=0):
    """
    Cover Art Archive via MusicBrainz.
    Cleans up folder-style names (e.g. '-Some-Band-' → 'Some Band')
    and strips year suffixes from album names (e.g. 'Album Title (1984)' → 'Album Title')
    before querying so special characters don't break Lucene syntax.
    Tries two query variants if the first returns nothing.
    """
    if page:
        return []
    a_clean  = _clean_artist(artist)
    al_clean = _clean_album(album)
    if not a_clean and not al_clean:
        return []

    def _mb_search(a, al, limit=3):
        q = f'artist:"{a}" release:"{al}"' if a else f'release:"{al}"'
        return _get_json('https://musicbrainz.org/ws/2/release/',
            params={'query': q, 'fmt': 'json', 'limit': limit},
            headers={'User-Agent': _MB_UA}, timeout=12).get('releases', [])

    # Try artist+album first, then just album if no results. If MusicBrainz
    # itself is down this raises, and CAA shows as unavailable.
    releases = _mb_search(a_clean, al_clean) or _mb_search('', al_clean)
    releases = releases[:3]   # limit to 3 to keep latency down

    out = []
    for rel in releases:
        mbid = rel.get('id')
        if not mbid: continue
        try:
            time.sleep(0.1)   # MB courtesy rate limit
            caa = requests.get(
                f'https://coverartarchive.org/release/{mbid}',
                headers={'Accept': 'application/json', 'User-Agent': _MB_UA},
                timeout=10, allow_redirects=True)
            if caa.status_code == 200:
                for img in caa.json().get('images', []):
                    if img.get('front') or 'Front' in img.get('types', []):
                        full  = img.get('image', '')
                        thumb = (img.get('thumbnails', {}).get('500')
                                 or img.get('thumbnails', {}).get('large', ''))
                        if full:
                            date = (rel.get('date') or '')[:4]
                            ctry = rel.get('country', '')
                            sfx  = f" ({date}{', '+ctry if ctry else ''})"
                            out.append({'url': full, 'thumb': thumb or full,
                                        'label': rel.get('title', album) + sfx,
                                        'source': 'CAA', 'quality': 'scan'})
                        break
        except Exception:
            continue   # one release without art (404) or a hiccup doesn't sink the rest
    return out

def _deezer_artist(name, page):
    data = _get_json('https://api.deezer.com/search/artist',
        params={'q': name, 'index': page * 12, 'limit': 12}, timeout=10)
    out = []
    for a in data.get('data', []):
        pic = a.get('picture_xl') or a.get('picture_big') or a.get('picture', '')
        if _deezer_real_image(pic):
            out.append({'url': pic, 'thumb': a.get('picture_medium', pic),
                        'label': a.get('name', ''), 'source': 'Deezer',
                        'quality': _size_from_url(pic) or '500px'})
    return out

def _fanart_artist(name, page=0):
    key = cfg.get('fanart_key', '')
    if page or not key:
        return []
    artists = _get_json('https://musicbrainz.org/ws/2/artist/',
        params={'query': f'"{name}"', 'fmt': 'json', 'limit': 3},
        headers={'User-Agent': _MB_UA}, timeout=12).get('artists', [])
    if not artists: return []
    mbid = artists[0].get('id')
    if not mbid: return []
    time.sleep(0.2)
    r2 = requests.get(f'https://webservice.fanart.tv/v3/music/{mbid}',
        params={'api_key': key}, timeout=12)
    if r2.status_code == 404:
        return []   # artist simply not on fanart.tv
    r2.raise_for_status()
    data = r2.json()
    out = []
    for img in data.get('artistthumb', []):
        u = img.get('url', '')
        if u: out.append({'url': u, 'thumb': u, 'label': 'artist thumb',
                          'source': 'Fanart.tv', 'quality': '1000px'})
    for img in data.get('artistbackground', []):
        u = img.get('url', '')
        if u: out.append({'url': u, 'thumb': u, 'label': 'background',
                          'source': 'Fanart.tv', 'quality': '1920px'})
    return out

# ── Web image search (best effort) ────────────────────────────────────────────
#
# Neither DuckDuckGo nor Bing offers a keyless official image-search API. The
# `ddgs` package scrapes the same unofficial endpoints their websites use, so
# it can break, change, or be rate-limited / bot-challenged at any time (DDG
# routinely answers datacenter IPs with a 202 challenge and no results).
# So web search is best effort:
#   - DuckDuckGo and Bing are queried in parallel; either one is enough
#   - short timeout; if both fail the source shows "unavailable", never an error
#   - results are relevance-filtered and always placed after official sources
# For something sturdier, add a Provider for a keyed API (e.g. Brave Search
# API) to PROVIDERS below.

_DDG_TIMEOUT = 8
_WEB_PER_PAGE = 20
_WEB_ENGINES = (('duckduckgo', 'DuckDuckGo'), ('bing', 'Bing'))

def _ddgs_images(query, page, backend):
    try:
        from ddgs import DDGS
    except ImportError:
        raise RuntimeError("the 'ddgs' package isn't installed — run: pip install ddgs")
    return DDGS(timeout=_DDG_TIMEOUT).images(
        query, max_results=_WEB_PER_PAGE, page=page + 1,
        backend=backend, safesearch='moderate') or []

def _web_images(query, page=0):
    """DuckDuckGo + Bing image results (each labelled with its engine). Raises only if all engines fail."""
    futs = [(label, _search_pool.submit(_ddgs_images, query, page, be)) for be, label in _WEB_ENGINES]
    out, errors = [], []
    for label, fut in futs:
        try:
            raw = fut.result(timeout=_DDG_TIMEOUT + 2)
        except Exception as e:
            errors.append(f'{label}: {_short_error(e)}')
            continue
        for item in raw:
            img = item.get('image') or ''
            if not img.startswith(('http://', 'https://')):
                continue
            w, h = item.get('width') or 0, item.get('height') or 0
            try: q = f'{max(int(w), int(h))}px' if w and h else (_size_from_url(img) or '')
            except (TypeError, ValueError): q = _size_from_url(img) or ''
            out.append({'url': img, 'thumb': item.get('thumbnail') or img,
                        'label': item.get('title', ''), 'page_url': item.get('url', ''),
                        'source': label, 'quality': q})
    if not out and len(errors) == len(_WEB_ENGINES):
        raise RuntimeError('; '.join(errors))
    return out

def _web_album(artist, album, page=0):
    return _filter_by_relevance(_web_images(f'{artist} {album} album cover', page), artist, album)

def _web_artist(name, page=0):
    return _filter_by_relevance(_web_images(f'{name} artist photo', page), name, '')

# ── Provider registry + aggregator ────────────────────────────────────────────

class Provider:
    """
    One image source. fn(*query_args, page) -> [result]; raises on failure.
    kind:  'album' (args: artist, album) or 'artist' (args: name)
    phase: album sources are split into a 'fast' phase shown immediately and
           a 'slow' phase merged in when ready
    web:   True for general web search — results go after the official ones
    enabled(cfg) decides whether to call it at all.
    """
    def __init__(self, key, label, kind, fn, phase='fast', web=False,
                 enabled=lambda c: True, timeout=15, hint=''):
        self.key, self.label, self.kind, self.fn = key, label, kind, fn
        self.phase, self.web, self.enabled = phase, web, enabled
        self.timeout, self.hint = timeout, hint

# Order within a phase is display priority.
PROVIDERS = [
    Provider('itunes',    'iTunes',    'album',  _itunes_album),
    Provider('discogs',   'Discogs',   'album',  _discogs_album,
             enabled=lambda c: bool(c.get('discogs_token')), hint='needs a free personal token'),
    Provider('deezer',    'Deezer',    'album',  _deezer_album),
    Provider('caa',       'CAA',       'album',  _caa_album, phase='slow', timeout=22),
    Provider('web',       'Web',       'album',  _web_album, phase='slow', web=True,
             enabled=lambda c: c.get('web_search', True), timeout=_DDG_TIMEOUT + 4,
             hint='DuckDuckGo + Bing images (unofficial, best effort)'),
    Provider('fanart',    'Fanart.tv', 'artist', _fanart_artist,
             enabled=lambda c: bool(c.get('fanart_key')), hint='needs a free API key'),
    Provider('wikipedia', 'Wikipedia', 'artist', _wikipedia_artist),
    Provider('deezer',    'Deezer',    'artist', _deezer_artist),
    Provider('web',       'Web',       'artist', _web_artist, web=True,
             enabled=lambda c: c.get('web_search', True), timeout=_DDG_TIMEOUT + 4,
             hint='DuckDuckGo + Bing images (unofficial, best effort)'),
]

def _short_error(e):
    msg = str(e) or type(e).__name__
    if isinstance(e, requests.exceptions.Timeout) or 'timed out' in msg.lower():
        return 'timed out'
    if 'ratelimit' in type(e).__name__.lower() or '429' in msg or 'rate' in msg.lower():
        return 'rate limited'
    if isinstance(e, requests.exceptions.ConnectionError):
        return "can't connect"
    return msg.splitlines()[0][:120]

_search_pool = concurrent.futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix='search')

def run_providers(kind, phase, args, page):
    """
    Query every enabled provider of this kind/phase in parallel.
    Returns (results, sources) where sources is
      [{key, label, status: ok|unavailable|disabled, count, message?, web, hint}]
    Each provider gets its own deadline; a slow or failing one is reported as
    unavailable and never delays the response beyond its timeout.
    """
    provs = [p for p in PROVIDERS if p.kind == kind and (phase is None or p.phase == phase)]
    futures, sources = {}, []
    start = time.monotonic()
    for p in provs:
        src = {'key': p.key, 'label': p.label, 'web': p.web, 'phase': p.phase,
               'hint': p.hint, 'count': 0}
        sources.append(src)
        if not p.enabled(cfg):
            src['status'] = 'disabled'
            continue
        futures[p.key] = (p, src, _search_pool.submit(p.fn, *args, page))
    official, web = [], []
    for key, (p, src, fut) in futures.items():
        remaining = max(0.0, p.timeout - (time.monotonic() - start))
        try:
            res = fut.result(timeout=remaining) or []
            src['status'], src['count'] = 'ok', len(res)
            (web if p.web else official).extend(res)
        except concurrent.futures.TimeoutError:
            src['status'], src['message'] = 'unavailable', 'timed out'
        except Exception as e:
            src['status'], src['message'] = 'unavailable', _short_error(e)
    for r in web:
        r['web'] = True
    official = _dedup(official)
    seen = {r['url'] for r in official}
    return official + [r for r in _dedup(web) if r['url'] not in seen], sources
