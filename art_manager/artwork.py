"""Reading/writing artwork: cover.jpg, artist.jpg, and pictures embedded in FLACs."""
import base64, io

import requests
from mutagen.flac import FLAC, Picture

from . import storage
from .scan import list_dir


def to_data_url(data, mime):
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def dl_image(url):
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
    return [storage.join(dir_path, f) for f in sorted(files) if f.lower().endswith('.flac')]


def collect_album_flacs(disc_info):
    flacs, seen = [], set()
    for disc in disc_info:
        if disc['path'] in seen:
            continue
        seen.add(disc['path'])
        try:
            flacs.extend(_flacs_in(disc['path']))
        except Exception:
            pass
    return flacs


def read_flac(path):
    with storage.open_file(path, 'rb') as fh:
        return FLAC(fh)


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
                audio = read_flac(f)
            except Exception:
                continue
            if audio.pictures:
                return audio.pictures[0].data, audio.pictures[0].mime
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


def write_cover(dir_path, jpeg):
    """Write cover.jpg into one folder, reusing an existing Cover.JPG-style name if there is one."""
    from .scan import COVER_JPG, find_cover_jpg
    files, _ = list_dir(dir_path)
    p = find_cover_jpg(dir_path, files) or storage.join(dir_path, COVER_JPG)
    storage.write_bytes(p, jpeg)
    return p

def write_covers(album_path, disc_info, jpeg):
    """
    cover.jpg in the album folder and in every disc folder.
    -> (album cover path, [{'path': disc folder, 'cover_path': its cover.jpg}])
    """
    root = write_cover(album_path, jpeg)
    discs = [{'path': d['path'], 'cover_path': write_cover(d['path'], jpeg)}
             for d in disc_info if d.get('path') and d['path'] != album_path]
    return root, discs

def write_artist_img(artist_path, data, mime):
    ext = 'png' if 'png' in mime else 'jpg'
    p = storage.join(artist_path, f'artist.{ext}')
    storage.write_bytes(p, data)
    return p


def embed_into(flac_path, data, mime):
    """Replace the pictures in one FLAC with this front cover."""
    pic = Picture()
    pic.type = 3; pic.mime = mime; pic.data = data
    # One read+write handle for load and save. Local files and SMB edit in
    # place; WebDAV hands back an in-memory copy that is uploaded on close.
    # save() re-reads the header to sanity-check it, so the handle must be
    # rewound first — load() leaves the position after the metadata.
    with storage.open_file(flac_path, 'rb+') as f:
        audio = FLAC(f)
        audio.clear_pictures(); audio.add_picture(pic)
        f.seek(0)
        audio.save(f)
