"""
Library scanning.

Layout understood:  root / Artist / Album / *.flac
                    root / Artist / Album / Disc / *.flac   (multi-disc)

Incremental scan. Each artist in the cache records the fingerprints (ETag or
mtime, see storage.Entry.fingerprint) of every directory the scan looked at:

    artist['fp']    the artist folder itself (seen in the root listing)
    artist['tree']  {subdir: [fp, {subsubdir: fp} | None]}

A folder's fingerprint changes when an entry is added/removed/renamed directly
inside it — NOT when something changes deeper down. So an artist is reused
from cache only if its own fingerprint, the fingerprint of every subfolder,
and (for subfolders that have no tracks directly inside: multi-disc albums and
not-yet-albums) every sub-subfolder all still match. Checking that costs one
listing of the artist folder plus one per such subfolder; a cold scan costs
exactly one listing per directory.

A fingerprint of None means the backend can't tell (e.g. a WebDAV server
without ETags) — that folder is always rescanned.

Errors: an unreadable root fails the scan. An unreadable artist/album/disc is
reported in `errors`, the last known version is kept if there is one, and the
artist is marked dirty so the next scan never trusts the cache for it.
"""
import concurrent.futures, threading, time

from . import storage
from .config import is_ignored

# The rule: every album folder AND every disc folder (CD 1, CD 2, ...) must hold
# a cover.jpg (any capitalisation). Other image files are only shown as the
# "current cover" and can be turned into cover.jpg; they don't count as done.
COVER_JPG        = 'cover.jpg'
OTHER_COVERS     = ('cover.jpeg', 'cover.png', 'folder.jpg', 'folder.jpeg', 'folder.png',
                    'front.jpg', 'front.png')
ARTIST_IMG_NAMES = ('artist.jpg', 'artist.png', 'Artist.jpg', 'Artist.png')

# Bump when the meaning of cached fields changes: an older cache is then
# ignored once and the library fully rescanned.
SCHEMA_VERSION = 2

LISTDIR_RETRY_DELAYS = (0.3, 1.0)
MAX_WORKERS = 8   # keep small: SMB servers and the smbclient pool don't like more


def list_dir(path):
    """
    One listing with retries for transient failures -> (files {name: Entry}, dirs {name: Entry}).
    Ignored names are dropped. Raises if the folder still can't be read.
    """
    for delay in LISTDIR_RETRY_DELAYS + (None,):
        try:
            entries = storage.scandir(path)
            break
        except Exception as e:
            if delay is None or storage.is_permanent(e):
                raise
            time.sleep(delay)
    files, dirs = {}, {}
    for e in entries:
        if e.is_dir:
            if not is_ignored(e.name):
                dirs[e.name] = e
        else:
            files[e.name] = e
    return files, dirs


def _find(dir_path, files, names):
    for n in names:
        if n in files:
            return storage.join(dir_path, n)
    return None

def find_cover_jpg(dir_path, files):
    """Path of the folder's cover.jpg (any case), or None."""
    for n in files:
        if n.lower() == COVER_JPG:
            return storage.join(dir_path, n)
    return None

def _any_cover(dir_path, files):
    """cover.jpg if present, else another cover-like image to show, else None."""
    found = find_cover_jpg(dir_path, files)
    if found:
        return found
    lower = {n.lower(): n for n in files}
    for n in OTHER_COVERS:
        if n in lower:
            return storage.join(dir_path, lower[n])
    return None

def _flac_count(files):
    return sum(1 for f in files if f.lower().endswith('.flac'))


def scan_library(root, prev_artists=None, errors=None, skipped=None):
    """
    Return the list of artists under root. Unreadable folders are appended to
    errors. Top-level folders that hold no album at all (playlists, scans,
    downloads...) are left out and their names appended to skipped.
    """
    if errors is None:
        errors = []
    if skipped is None:
        skipped = []
    _, root_dirs = list_dir(root)   # root unreadable -> raise
    if not root_dirs:
        return []
    prev_by_path = {a['path']: a for a in (prev_artists or [])}
    err_lock = threading.Lock()

    def scan_one(name):
        entry = root_dirs[name]
        artist_path = storage.join(root, name)
        cached = prev_by_path.get(artist_path)
        my_errors = []
        try:
            result = _scan_artist(artist_path, name, entry.fingerprint, cached, my_errors)
        except Exception as e:
            my_errors.append({'path': artist_path, 'message': storage.error_message(e, artist_path)})
            result = dict(cached, dirty=True) if cached else None   # keep last known state
        if my_errors:
            with err_lock:
                errors.extend(my_errors)
        return result

    names = sorted(root_dirs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(names))) as ex:
        results = list(ex.map(scan_one, names))
    out = []
    for name, a in zip(names, results):
        if a is None:
            continue
        if not a['albums'] and not a.get('dirty'):
            skipped.append(name)
            continue
        out.append(a)
    return out


def _tree_unchanged(artist_path, cached, a_dirs):
    """Compare a fresh listing of the artist folder (a_dirs) with the cached tree."""
    tree = cached.get('tree')
    if tree is None or set(tree) != set(a_dirs):
        return False
    for name, (fp, sub) in tree.items():
        if fp is None or a_dirs[name].fingerprint != fp:
            return False
        if sub is not None:
            _, s_dirs = list_dir(storage.join(artist_path, name))
            if set(sub) != set(s_dirs):
                return False
            if any(f is None or s_dirs[n].fingerprint != f for n, f in sub.items()):
                return False
    return True


def _scan_artist(artist_path, name, fp, cached, errors):
    a_files, a_dirs = list_dir(artist_path)

    if (cached and not cached.get('dirty') and fp is not None and cached.get('fp') == fp
            and _tree_unchanged(artist_path, cached, a_dirs)):
        return cached   # artist and every album/disc folder confirmed unchanged

    prev_albums = {al['path']: al for al in (cached or {}).get('albums', [])}
    albums, tree = [], {}
    for album_name in sorted(a_dirs):
        album_path = storage.join(artist_path, album_name)
        try:
            album, sub = _scan_album(album_path, album_name)
            tree[album_name] = [a_dirs[album_name].fingerprint, sub]
        except Exception as e:
            errors.append({'path': album_path, 'message': storage.error_message(e, album_path)})
            album = prev_albums.get(album_path)   # keep what we knew, if anything
            tree[album_name] = [None, None]       # forces a rescan next time
        if album:
            albums.append(album)

    img_path = _find(artist_path, a_files, ARTIST_IMG_NAMES)
    out = {'name': name, 'path': artist_path, 'has_img': img_path is not None,
           'img_path': img_path, 'albums': albums, 'fp': fp, 'tree': tree}
    if errors:
        out['dirty'] = True
    return out


def _scan_album(album_path, album_name):
    """
    -> (album dict or None if it holds no FLACs, sub-fingerprints or None).
    Raises if the album folder or one of its disc folders can't be read: a disc
    we can't read must not silently shrink the album.
    """
    al_files, al_dirs = list_dir(album_path)
    n_direct = _flac_count(al_files)
    root_jpg = find_cover_jpg(album_path, al_files)
    root_any = _any_cover(album_path, al_files)
    disc_info, sub = [], None

    if n_direct:
        disc_info = [{'name': None, 'path': album_path, 'flac_count': n_direct,
                      'has_cover': root_jpg is not None, 'cover_path': root_any}]
    else:
        sub = {}
        for d in sorted(al_dirs):
            d_path = storage.join(album_path, d)
            sub[d] = al_dirs[d].fingerprint
            d_files, _ = list_dir(d_path)
            n = _flac_count(d_files)
            if n:
                disc_info.append({'name': d, 'path': d_path, 'flac_count': n,
                                  'has_cover': find_cover_jpg(d_path, d_files) is not None,
                                  'cover_path': _any_cover(d_path, d_files)})
    if not disc_info:
        return None, sub

    is_multi = not n_direct
    # Done = cover.jpg in the album folder and, for multi-disc albums, in every disc folder.
    complete = root_jpg is not None and all(d['has_cover'] for d in disc_info)
    best = root_any or next((d['cover_path'] for d in disc_info if d['cover_path']), None)
    return {
        'name': album_name, 'path': album_path,
        'is_multi_disc': is_multi, 'disc_info': disc_info,
        'has_cover': complete,           # every required cover.jpg exists
        'cover_path': best,              # best image to show as "current cover" (may be folder.jpg etc.)
        'cover_in_root': root_jpg is not None,
        'total_tracks': sum(d['flac_count'] for d in disc_info),
    }, sub


def apply_ignore(lib):
    """Drop ignored artists/albums from a library dict (e.g. one cached before the folder was ignored)."""
    if not lib or not lib.get('artists'):
        return lib
    artists = []
    for a in lib['artists']:
        if is_ignored(a['name']):
            continue
        albums = [al for al in a.get('albums', []) if not is_ignored(al['name'])]
        artists.append(dict(a, albums=albums) if len(albums) != len(a.get('albums', [])) else a)
    return dict(lib, artists=artists)


def client_view(lib):
    """The library as sent to the browser: without the internal fingerprint data."""
    if not lib or not lib.get('artists'):
        return lib or {}
    return dict(lib, artists=[{k: v for k, v in a.items() if k not in ('tree', 'fp')}
                              for a in lib['artists']])
