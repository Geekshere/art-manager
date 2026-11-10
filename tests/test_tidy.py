"""cover.png / Cover.JPEG / cover.webp / a PNG named cover.jpg -> one proper cover file."""
import io, time

from PIL import Image

from art_manager import artwork, scan
from art_manager.config import cfg

def img(fmt, size=(600, 600), color='red'):
    b = io.BytesIO()
    Image.new('RGB', size, color).save(b, fmt)
    return b.getvalue()

def album(d):
    return {'path': str(d), 'disc_info': [{'path': str(d)}]}

def names(d):
    return sorted(p.name for p in d.iterdir())

def test_png_only_becomes_real_jpeg_and_png_is_removed(tmp_path):
    (tmp_path / 'Cover.PNG').write_bytes(img('PNG'))
    r = artwork.tidy_album(album(tmp_path))
    assert names(tmp_path) == ['cover.jpg']
    assert (tmp_path / 'cover.jpg').read_bytes()[:3] == b'\xff\xd8\xff'
    assert len(r['removed']) == 1 and len(r['written']) == 1

def test_jpeg_extension_is_copied_unchanged(tmp_path):
    data = img('JPEG')
    (tmp_path / 'cover.jpeg').write_bytes(data)
    artwork.tidy_album(album(tmp_path))
    assert names(tmp_path) == ['cover.jpg'] and (tmp_path / 'cover.jpg').read_bytes() == data

def test_existing_cover_jpg_is_kept_and_duplicate_removed(tmp_path):
    mine = img('JPEG', (500, 500), 'blue')
    (tmp_path / 'cover.jpg').write_bytes(mine)
    (tmp_path / 'cover.png').write_bytes(img('PNG', (1400, 1400)))
    artwork.tidy_album(album(tmp_path))
    assert names(tmp_path) == ['cover.jpg'] and (tmp_path / 'cover.jpg').read_bytes() == mine

def test_biggest_variant_wins_when_there_is_no_cover_jpg(tmp_path):
    (tmp_path / 'cover.png').write_bytes(img('PNG', (300, 300)))
    (tmp_path / 'cover.webp').write_bytes(img('WEBP', (1200, 1200)))
    artwork.tidy_album(album(tmp_path))
    assert names(tmp_path) == ['cover.jpg']
    assert Image.open(tmp_path / 'cover.jpg').size == (1200, 1200)

def test_png_named_jpg_is_reencoded(tmp_path):
    (tmp_path / 'cover.jpg').write_bytes(img('PNG'))
    r = artwork.tidy_album(album(tmp_path))
    assert r['written'] and Image.open(tmp_path / 'cover.jpg').format == 'JPEG'

def test_keep_old_files_when_asked(tmp_path):
    (tmp_path / 'cover.png').write_bytes(img('PNG'))
    r = artwork.tidy_album(album(tmp_path), remove=False)
    assert names(tmp_path) == ['cover.jpg', 'cover.png'] and not r['removed']

def test_unreadable_image_is_never_deleted(tmp_path):
    (tmp_path / 'cover.png').write_bytes(b'not an image')
    r = artwork.tidy_album(album(tmp_path))
    assert names(tmp_path) == ['cover.png'] and not r['written']

def test_png_cover_name_converts_the_other_way(tmp_path, monkeypatch):
    monkeypatch.setitem(cfg, 'cover_name', 'cover.png')
    (tmp_path / 'cover.jpg').write_bytes(img('JPEG'))
    artwork.tidy_album(album(tmp_path))
    assert names(tmp_path) == ['cover.png'] and Image.open(tmp_path / 'cover.png').format == 'PNG'

def test_artist_image(tmp_path):
    (tmp_path / 'Artist.jpeg').write_bytes(img('JPEG'))
    artwork.tidy_artist(str(tmp_path))
    assert names(tmp_path) == ['artist.jpg']

def test_jellyfin_folder_names(tmp_path, monkeypatch):
    monkeypatch.setitem(cfg, 'cover_name', 'folder.jpg')
    (tmp_path / 'Folder.png').write_bytes(img('PNG'))
    (tmp_path / 'cover.png').write_bytes(img('PNG'))          # another stem: left alone
    artwork.tidy_album(album(tmp_path))
    assert names(tmp_path) == ['cover.png', 'folder.jpg']

def test_scan_reports_variants(make_library):
    root = make_library()
    d = root / 'Alpha' / 'Second'
    (d / 'cover.PNG').write_bytes(img('PNG'))
    (root / 'Alpha' / 'artist.png').write_bytes(img('PNG'))
    lib = scan.scan_library(str(root))
    a = next(x for x in lib if x['name'] == 'Alpha')
    al = next(x for x in a['albums'] if x['name'] == 'Second')
    assert al['cover_variants'] == 1 and not al['has_cover'] and al['cover_path'].endswith('cover.PNG')
    assert a['img_variants'] == 1

def test_tidy_job_and_png_named_jpg_served_as_png(client, make_library):
    root = make_library()
    d = root / 'Alpha' / 'Second'
    (d / 'cover.jpg').write_bytes(img('JPEG'))
    (d / 'cover.png').write_bytes(img('PNG'))
    client.get('/api/scan', query_string={'root': str(root)})
    job = client.post('/api/fix-covers', json={'mode': 'tidy', 'albums': [album(d)]}).json
    for _ in range(100):
        p = client.get(f'/api/fix-covers/{job["id"]}').json
        if p['status'] != 'running':
            break
        time.sleep(0.05)
    assert p['counts'] == {'fixed': 1} and not (d / 'cover.png').exists()
    (d / 'cover.jpg').write_bytes(img('PNG'))
    r = client.get('/api/image/local', query_string={'path': str(d / 'cover.jpg')})
    assert r.mimetype == 'image/png'
