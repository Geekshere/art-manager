"""Reading/writing artwork: cover.jpg, artist.jpg, and pictures embedded in the tracks (any format)."""
import base64, io

import requests
from . import formats, storage
from .config import cfg
from .scan import is_track, list_dir


def to_data_url(data, mime):
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def dl_image(url):
    if url.startswith('/api/image/redacted'):
        # an image on Redacted's own host: fetched with credentials that stay on this server
        from urllib.parse import parse_qs, urlsplit
        from . import redacted
        data, ct = redacted.fetch_site_image(parse_qs(urlsplit(url).query).get('path', [''])[0])
        return data, ct if ct in ('image/jpeg', 'image/png', 'image/webp') else 'image/jpeg'
    if not url.startswith(('http://', 'https://')):
        raise ValueError('Not a web address: ' + url[:100])
    r = requests.get(url, timeout=25, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    ct = r.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
    mime = ct if ct in ('image/jpeg', 'image/png', 'image/webp') else 'image/jpeg'
    return r.content, mime


def decode_uploaded_image(data_url):
    """Decode a browser-side 'data:image/jpeg;base64,...' upload into (bytes, mime)."""
    if not isinstance(data_url, str) or not data_url.startswith('data:'):
        raise ValueError('No image data received')
    header, _, b64data = data_url.partition(',')
    if not b64data:
        raise ValueError('No image data received')
    mime = header[len('data:'):].split(';')[0] or 'image/jpeg'
    if mime not in ('image/jpeg', 'image/png'):
        raise ValueError(f'Unsupported image type: {mime}')
    return base64.b64decode(b64data), mime


def _flacs_in(dir_path):
    files, _ = list_dir(dir_path)
    return [storage.join(dir_path, f) for f in sorted(files) if is_track(f)]


def collect_album_flacs(disc_info):
    flacs, seen = [], set()
    for disc in disc_info:
        if disc['path'] in seen:
            continue
        seen.add(disc['path'])
        try:
            flacs.extend(p for p in _flacs_in(disc['path']) if formats.can_embed(p))
        except Exception:
            pass
    return flacs


def read_audio(path):
    return formats.open_audio(path)

read_flac = read_audio   # old name

def track_pictures(path):
    """[{'type', 'mime', 'data'}] embedded in one track."""
    return formats.pictures(formats.open_audio(path), path)


def find_album_embedded(album_path, disc_info):
    """(data, mime) of the first picture embedded in any track of the album, or (None, None)."""
    paths = [album_path] + [d['path'] for d in disc_info if d['path'] != album_path]
    for dpath in dict.fromkeys(paths):
        try:
            flacs = _flacs_in(dpath)
        except Exception:
            continue
        for f in flacs:
            try:
                pics = track_pictures(f)
            except Exception:
                continue
            if pics:
                front = next((p for p in pics if p['type'] == 3), pics[0])
                return front['data'], front['mime'] or 'image/jpeg'
    return None, None


def to_jpeg(data, mime):
    """
    The image as JPEG bytes. JPEGs are passed through untouched (no
    recompression); PNG/WebP/etc. are converted at quality 95, with any
    transparency flattened onto white.
    """
    if mime == 'image/jpeg' and data[:3] == b'\xff\xd8\xff':
        return data
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise ValueError(f'Not a readable image ({mime}): {e}')
    if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):
        im = im.convert('RGBA')
        flat = Image.new('RGB', im.size, (255, 255, 255))
        flat.paste(im, mask=im.getchannel('A'))
        im = flat
    else:
        im = im.convert('RGB')
    out = io.BytesIO()
    im.save(out, 'JPEG', quality=95, optimize=True)
    return out.getvalue()


def encode_for(name, jpeg):
    """JPEG bytes -> the format the file name asks for (.png gets PNG)."""
    if not name.lower().endswith('.png'):
        return jpeg
    from PIL import Image
    out = io.BytesIO()
    Image.open(io.BytesIO(jpeg)).save(out, 'PNG', optimize=True)
    return out.getvalue()

def _write_named(dir_path, name, jpeg, find):
    """Write the image under `name` (reusing an existing differently-cased file), in name's format."""
    files, _ = list_dir(dir_path)
    p = find(dir_path, files) or storage.join(dir_path, name)
    storage.write_bytes(p, encode_for(name, jpeg))
    return p

def write_cover(dir_path, jpeg):
    """Write the configured cover file (cover.jpg, folder.jpg, ...) into one folder."""
    from .scan import find_cover
    return _write_named(dir_path, cfg.get('cover_name') or 'cover.jpg', jpeg, find_cover)

def cover_folders(album_path, disc_info):
    """Folders that need the cover file: the album folder, plus disc folders if configured."""
    folders = [album_path]
    if cfg.get('disc_covers', True):
        folders += [d['path'] for d in disc_info if d.get('path') and d['path'] != album_path]
    return folders

def write_covers(album_path, disc_info, jpeg):
    """
    The cover file in the album folder and (if configured) every disc folder.
    -> (album cover path, [{'path': disc folder, 'cover_path': its cover}])
    """
    root = write_cover(album_path, jpeg)
    discs = [{'path': f, 'cover_path': write_cover(f, jpeg)} for f in cover_folders(album_path, disc_info)[1:]]
    return root, discs

def write_artist_img(artist_path, data, mime):
    from .scan import find_artist_image
    return _write_named(artist_path, cfg.get('artist_image_name') or 'artist.jpg', to_jpeg(data, mime),
                        find_artist_image)


def embed_into(path, data, mime):
    """
    Replace the pictures in one track with this front cover. One read+write
    handle for load and save: local files and SMB edit in place; WebDAV hands
    back an in-memory copy that is uploaded on close.
    """
    formats.embed(path, data, mime)


# ── Wrong type / extension ────────────────────────────────────────────────────

def sniff_image(data):
    return ('image/jpeg' if data[:3] == b'\xff\xd8\xff' else 'image/png' if data[:8] == b'\x89PNG\r\n\x1a\n'
            else 'image/webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else 'image/gif' if data[:3] == b'GIF'
            else 'image/bmp' if data[:2] == b'BM' else 'image/tiff' if data[:4] in (b'II*\x00', b'MM\x00*') else None)

def _image_info(path):
    """(bytes, real mime, pixels) of an image file, or None if it isn't a readable image."""
    from PIL import Image
    data = storage.read_bytes(path)
    mime = sniff_image(data)
    try:
        w, h = Image.open(io.BytesIO(data)).size
    except Exception:
        return None
    return data, mime, w * h

def _same_picture(a, b, tolerance=14):
    """Whether two images show the same picture (any size/format): 16×16 grey thumbnails compared."""
    from PIL import Image
    try:
        ia, ib = (Image.open(io.BytesIO(x)) for x in (a, b))
        ra, rb = ia.size[0] / ia.size[1], ib.size[0] / ib.size[1]
        if abs(ra - rb) > 0.05:
            return False
        ta, tb = (i.convert('L').resize((16, 16)).tobytes() for i in (ia, ib))
    except Exception:
        return False
    return sum(abs(x - y) for x, y in zip(ta, tb)) / 256 <= tolerance

def tidy_folder(folder, required, find, files=None, remove=True):
    """
    Make `required` (cover.jpg, artist.jpg, folder.jpg, ...) the one proper
    image of its kind in a folder:
      - cover.png / Cover.JPEG / cover.webp / ... and no cover.jpg -> converted
        into cover.jpg (JPEGs are copied unchanged, others converted at q95)
      - several of them and no cover.jpg -> the one with the most pixels
      - cover.jpg and cover.png both there -> the bigger one if they show the
        same picture, else the existing cover.jpg
      - cover.jpg that is really a PNG (or anything but a JPEG) -> re-encoded
      - with remove=True the other spellings are deleted
        afterwards, so players can't pick the old one. Unreadable images are
        never deleted, and nothing is deleted unless the new file was written.
    -> {'written': path or None, 'removed': [paths], 'kept': [paths]}
    """
    from .scan import variants
    if files is None:
        files = list_dir(folder)[0]
    have = find(folder, files)
    others = variants(folder, files, required)
    out = {'written': None, 'removed': [], 'kept': []}
    want_png = required.endswith('.png')
    want = 'image/png' if want_png else 'image/jpeg'
    cand = []
    for p in ([have] if have else []) + others:
        try:
            info = _image_info(p)
        except Exception:
            info = None
        if info:
            cand.append((p, *info))
        elif p != have:
            out['kept'].append(p)          # not readable: leave it for the user to look at
    if not cand:
        return out
    # The biggest copy wins — but a copy that shows *different* art never
    # replaces an existing cover.jpg (that one is what players show and what
    # was picked in this app); then cover.jpg is kept.
    current = next((c for c in cand if c[0] == have), None)
    best = max(cand, key=lambda c: (c[3], c[0] == have))
    if current and best is not current and not _same_picture(current[1], best[1]):
        best = current
    path, data, mime, _ = best
    if path != have or mime != want:
        jpeg = to_jpeg(data, mime or 'image/jpeg')
        out['written'] = _write_named(folder, required, jpeg, find)
    if remove:
        for p, *_ in cand:
            if p != have and p != out['written']:
                try:
                    storage.remove(p)
                    out['removed'].append(p)
                except Exception:
                    out['kept'].append(p)
    else:
        out['kept'] += [p for p, *_ in cand if p != have and p != out['written']]
    return out

def tidy_album(album, remove=True):
    """tidy_folder() for the album folder and every disc folder that has such images."""
    from .scan import find_cover
    name = (cfg.get('cover_name') or 'cover.jpg').lower()
    root = album['path']
    res = {'written': [], 'removed': [], 'kept': []}
    for f in dict.fromkeys([root] + [d['path'] for d in album.get('disc_info', [])]):
        r = tidy_folder(f, name, find_cover, remove=remove)
        if r['written']:
            res['written'].append(r['written'])
        res['removed'] += r['removed']; res['kept'] += r['kept']
    return res

def tidy_artist(artist_path, remove=True):
    from .scan import find_artist_image
    name = (cfg.get('artist_image_name') or 'artist.jpg').lower()
    r = tidy_folder(artist_path, name, find_artist_image, remove=remove)
    return {'written': [r['written']] if r['written'] else [], 'removed': r['removed'], 'kept': r['kept']}


# ── Bulk fix ──────────────────────────────────────────────────────────────────

def fix_album_covers(album, embed=True, use_embedded=True, on_track=None, remove_old=True):
    """
    Make sure every folder that needs one (album folder, plus disc folders if
    configured) has the cover file, without ever replacing an existing one
    (discs may have their own art). Source for a folder that lacks it, in
    order: another cover image in that folder (folder.jpg, cover.png, ...),
    the album's cover, the art embedded in the tracks. With embed=True (and
    embedding on in Settings) every track gets its folder's cover.
    -> {'status': 'fixed'|'skipped', 'written': [paths], 'embedded': n, 'message'}
    """
    from .scan import _any_cover, find_cover
    tidied = tidy_album(album, remove=remove_old)   # cover.png / cover.jpeg -> cover.jpg first
    root = album['path']
    folders = [root] + [d['path'] for d in album.get('disc_info', []) if d['path'] != root]
    required = cover_folders(root, album.get('disc_info', []))
    listing = {f: list_dir(f)[0] for f in folders}
    have = {f: find_cover(f, listing[f]) for f in folders}
    other = {f: _any_cover(f, listing[f]) for f in folders}

    def load(path):
        data = storage.read_bytes(path)
        return to_jpeg(data, sniff_image(data) or 'image/jpeg')

    album_img = None
    for f in [root] + folders[1:]:            # album folder first, then the discs
        src = have[f] or other[f]
        if src:
            album_img = load(src)
            break
    if album_img is None and use_embedded:
        data, mime = find_album_embedded(root, album.get('disc_info', []))
        if data:
            album_img = to_jpeg(data, mime)
    if album_img is None:
        return {'status': 'fixed' if tidied['written'] or tidied['removed'] else 'skipped',
                'written': tidied['written'], 'removed': tidied['removed'], 'embedded': 0,
                'message': '' if tidied['written'] else 'no cover image or embedded art to use'}

    written, folder_img = list(tidied['written']), {}
    for f in folders:
        if have[f]:
            folder_img[f] = load(have[f])
        elif f in required:
            img = load(other[f]) if other[f] else album_img   # the folder's own art first
            written.append(write_cover(f, img))
            folder_img[f] = img

    embedded = 0
    if embed and cfg.get('embed_art', True):
        for f in folders:
            img = folder_img.get(f) or folder_img.get(root) or album_img
            for fl in _flacs_in(f):
                if not formats.can_embed(fl):
                    continue
                embed_into(fl, img, 'image/jpeg')
                embedded += 1
                if on_track:
                    on_track(fl)
    return {'status': 'fixed', 'written': written, 'removed': tidied['removed'], 'embedded': embedded, 'message': ''}
