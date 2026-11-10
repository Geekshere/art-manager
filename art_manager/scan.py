"""
Library scanning.

Layout understood:  root / Artist / Album / *.flac (or .mp3, .m4a, .ogg, .opus, ...)
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
from .config import cfg, is_ignored

# The rule: every album folder — and, if cfg['disc_covers'], every disc folder
# (CD 1, CD 2, ...) — must hold the configured cover file (cfg['cover_name'],
# e.g. cover.jpg or folder.jpg; any capitalisation). Other images are only
# shown as the "current cover" and can be turned into the right file; they
# don't count as done. Same for cfg['artist_image_name'] in artist folders.
# Any capitalisation and any of these image types counts as "another cover
# image" (cover.PNG, Cover.jpeg, folder.webp, ...).
IMAGE_EXTS = ('.jpg', '.jpeg', '.jfif', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff')
COVER_STEMS = ('cover', 'folder', 'front', 'album', 'albumart')
ARTIST_STEMS = ('artist', 'folder', 'poster')
OTHER_COVERS = tuple(st + e for st in COVER_STEMS for e in IMAGE_EXTS)
OTHER_ARTIST_IMAGES = tuple(st + e for st in ARTIST_STEMS for e in IMAGE_EXTS)

# Bump when the meaning of cached fields changes: an older cache is then
# ignored once and the library fully rescanned.
SCHEMA_VERSION = 5

def cover_name():
    return (cfg.get('cover_name') or 'cover.jpg').lower()

def artist_image_name():
    return (cfg.get('artist_image_name') or 'artist.jpg').lower()

def is_junk_file(name):
    """macOS '._' metadata files end in .flac / .mp3 too, but are not tracks."""
    return name.startswith('._')

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

def _named(dir_path, files, name):
    for n in files:
        if n.lower() == name:
            return storage.join(dir_path, n)
    return None

def find_cover(dir_path, files):
    """Path of the folder's required cover file (cfg cover_name, any case), or None."""
    return _named(dir_path, files, cover_name())

def find_artist_image(dir_path, files):
    return _named(dir_path, files, artist_image_name())

def _stem(name):
    return name.rsplit('.', 1)[0].lower()

def variants(dir_path, files, required):
    """
    Same name, other type or spelling of the required image: 'cover.png',
    'Cover.JPEG', 'cover.webp' when cover.jpg is wanted. Players often pick
    these up instead of (or as well as) the right file.
    """
    stem = _stem(required)
    return [storage.join(dir_path, n) for n in sorted(files)
            if n.lower() != required and _stem(n) == stem and n.lower().endswith(IMAGE_EXTS)
            and not n.startswith('._')]

def _first_of(dir_path, files, names, required):
    found = _named(dir_path, files, required)
    if found:
        return found
    same = variants(dir_path, files, required)     # cover.png before folder.jpg
    if same:
        return same[0]
    lower = {n.lower(): n for n in files if not n.startswith('._')}
    for n in names:
        if n in lower:
            return storage.join(dir_path, lower[n])
    return None

def _any_cover(dir_path, files):
    """The required cover file if present, else another cover-like image to show, else None."""
    return _first_of(dir_path, files, OTHER_COVERS, cover_name())

def _any_artist_image(dir_path, files):
    return _first_of(dir_path, files, OTHER_ARTIST_IMAGES, artist_image_name())

def is_track(name):
    from .formats import is_audio
    return is_audio(name) and not is_junk_file(name)

def _flac_count(files):
    return sum(1 for f in files if is_track(f))


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
    workers = min(MAX_WORKERS, storage.parallelism(root), len(names))
    if workers <= 1:
        results = [scan_one(n) for n in names]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
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

    required = find_artist_image(artist_path, a_files)
    img_path = required or _any_artist_image(artist_path, a_files)
    out = {'name': name, 'path': artist_path, 'has_img': required is not None,
           'img_path': img_path, 'img_variants': len(variants(artist_path, a_files, artist_image_name())),
           'albums': albums, 'fp': fp, 'tree': tree}
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
    root_jpg = find_cover(album_path, al_files)
    root_any = _any_cover(album_path, al_files)
    disc_info, sub = [], None

    if n_direct:
        disc_info = [{'name': None, 'path': album_path, 'flac_count': n_direct,
                      'has_cover': root_jpg is not None, 'cover_path': root_any}]
        n_variants = len(variants(album_path, al_files, cover_name()))
    else:
        sub = {}
        n_variants = len(variants(album_path, al_files, cover_name()))
        for d in sorted(al_dirs):
            d_path = storage.join(album_path, d)
            sub[d] = al_dirs[d].fingerprint
            d_files, _ = list_dir(d_path)
            n = _flac_count(d_files)
            if n:
                disc_info.append({'name': d, 'path': d_path, 'flac_count': n,
                                  'has_cover': find_cover(d_path, d_files) is not None,
                                  'cover_path': _any_cover(d_path, d_files)})
                n_variants += len(variants(d_path, d_files, cover_name()))
    if not disc_info:
        return None, sub

    is_multi = not n_direct
    # Done = the cover file in the album folder and, for multi-disc albums with
    # cfg['disc_covers'], in every disc folder too.
    complete = root_jpg is not None and (not is_multi or not cfg.get('disc_covers', True)
                                         or all(d['has_cover'] for d in disc_info))
    best = root_any or next((d['cover_path'] for d in disc_info if d['cover_path']), None)
    return {
        'name': album_name, 'path': album_path,
        'is_multi_disc': is_multi, 'disc_info': disc_info,
        'has_cover': complete,           # every required cover file exists
        'cover_path': best,              # best image to show as "current cover" (may be folder.jpg etc.)
        'cover_in_root': root_jpg is not None,
        'total_tracks': sum(d['flac_count'] for d in disc_info),
        'cover_variants': n_variants,    # cover.png / cover.jpeg / ... next to (or instead of) cover.jpg
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
