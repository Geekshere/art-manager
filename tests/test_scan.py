import os, shutil, time
import pytest
from art_manager import config, scan
from conftest import add_flac, fail_once, JPEG_BYTES

def names(lib):
    return sorted(a['name'] for a in lib['artists'])

def artist(lib, name):
    return next(a for a in lib['artists'] if a['name'] == name)

def album(lib, a, al):
    return next(x for x in artist(lib, a)['albums'] if x['name'] == al)

def do_scan(client, root, full=False):
    r = client.get('/api/scan', query_string={'root': str(root), **({'full': '1'} if full else {})})
    assert r.status_code == 200, r.json
    return r.json

def bump(path):
    """Force a new mtime (filesystems can have coarse timestamp resolution)."""
    t = time.time() + 5
    os.utime(path, (t, t))

def ndirs(root):
    """Directories the scanner lists: everything except ignored folders and their contents."""
    n = 0
    for _, dirs, _ in os.walk(root):
        dirs[:] = [d for d in dirs if not config.is_ignored(d)]
        n += 1
    return n

def test_full_scan_shapes(client, make_library):
    root = make_library()
    lib = do_scan(client, root)
    assert names(lib) == ['!!!', '100% Orange Juice', 'Alpha']   # !Playlists ignored by default
    assert lib['errors'] == []
    a = artist(lib, 'Alpha')
    assert a['has_img'] and 'tree' not in a and 'fp' not in a   # internal fields not sent
    assert album(lib, 'Alpha', 'First')['has_cover'] is True
    assert album(lib, 'Alpha', 'Second')['has_cover'] is False   # embedded only doesn't count
    m = album(lib, '100% Orange Juice', 'Multi')
    assert m['is_multi_disc'] and m['total_tracks'] == 2
    assert [d['has_cover'] for d in m['disc_info']] == [True, False]
    # CD 1 has cover.jpg, but the album folder and CD 2 don't -> not done
    assert m['has_cover'] is False and m['cover_in_root'] is False
    assert m['cover_path'].endswith('cover.jpg')   # CD 1's cover is still shown

def test_incremental_detects_every_kind_of_change(client, make_library, flac_bytes):
    root = make_library()
    do_scan(client, root)
    add_flac(root / 'Zeta' / 'Z1' / '01.flac', flac_bytes)          # new artist
    shutil.rmtree(root / '!!!')                                      # removed artist
    add_flac(root / 'Alpha' / 'Third' / '01.flac', flac_bytes)       # new album
    bump(root / 'Alpha')
    (root / '100% Orange Juice' / 'Multi' / 'CD 2' / 'cover.jpg').write_bytes(JPEG_BYTES)
    bump(root / '100% Orange Juice' / 'Multi' / 'CD 2')              # file inside an existing disc
    lib = do_scan(client, root)
    assert 'Zeta' in names(lib) and '!!!' not in names(lib)
    assert {al['name'] for al in artist(lib, 'Alpha')['albums']} == {'First', 'Second', 'Third'}
    assert [d['has_cover'] for d in album(lib, '100% Orange Juice', 'Multi')['disc_info']] == [True, True]

def test_folder_that_becomes_a_multidisc_album(client, make_library, flac_bytes):
    """A folder with no tracks yet gets a disc subfolder with FLACs: only the disc folder's mtime changes."""
    root = make_library()
    (root / 'Alpha' / 'Boxset' / 'CD 1').mkdir(parents=True)
    bump(root / 'Alpha')
    lib = do_scan(client, root)
    assert 'Boxset' not in {al['name'] for al in artist(lib, 'Alpha')['albums']}
    add_flac(root / 'Alpha' / 'Boxset' / 'CD 1' / '01.flac', flac_bytes)
    bump(root / 'Alpha' / 'Boxset' / 'CD 1')
    lib = do_scan(client, root)
    assert 'Boxset' in {al['name'] for al in artist(lib, 'Alpha')['albums']}

def test_cold_scan_is_one_listing_per_directory(client, make_library, fake):
    root = make_library()
    lib = do_scan(client, 'fake://' + str(root))
    assert lib['artists']
    # exactly one listing per directory (nothing in the fixture is ignored by default)
    listed = set(fake.calls)
    assert len(fake.calls) == len(listed), 'a directory was listed twice'
    assert len(fake.calls) == ndirs(root)

def test_warm_scan_reuses_cache_cheaply(client, make_library, fake):
    root = make_library()
    froot = 'fake://' + str(root)
    do_scan(client, froot)
    fake.calls.clear()
    do_scan(client, froot)
    # root + one listing per artist + one per folder without direct tracks (the multi-disc album)
    artists = [d for d in os.listdir(root) if not config.is_ignored(d)]
    assert len(fake.calls) == 1 + len(artists) + 1
    assert froot + '/Alpha/First' not in fake.calls   # album folders themselves are not re-listed

def test_full_rescan_ignores_cache(client, make_library, fake):
    root = make_library()
    froot = 'fake://' + str(root)
    do_scan(client, froot)
    fake.calls.clear()
    r = client.get('/api/scan', query_string={'root': froot, 'full': '1'})
    assert r.status_code == 200
    assert len(fake.calls) == ndirs(root)   # every folder re-read, like a cold scan

# ── cover.jpg rule: album folder AND every disc folder ─────────────────────────

def test_multidisc_needs_cover_jpg_in_every_folder(client, make_library):
    root = make_library()
    m = root / '100% Orange Juice' / 'Multi'
    (m / 'cover.jpg').write_bytes(JPEG_BYTES)                 # album folder: yes; CD 2: still no
    assert album(do_scan(client, root), '100% Orange Juice', 'Multi')['has_cover'] is False
    (m / 'CD 2' / 'cover.jpg').write_bytes(JPEG_BYTES)
    al = album(do_scan(client, root, full=True), '100% Orange Juice', 'Multi')
    assert al['has_cover'] is True and al['cover_in_root'] is True

def test_other_cover_names_shown_but_not_done(client, make_library):
    root = make_library()
    (root / 'Alpha' / 'Second' / 'folder.jpg').write_bytes(JPEG_BYTES)
    al = album(do_scan(client, root), 'Alpha', 'Second')
    assert al['has_cover'] is False and al['cover_path'].endswith('folder.jpg')

def test_cover_jpg_any_case_counts_and_is_overwritten_in_place(client, make_library):
    root = make_library()
    d = root / 'Alpha' / 'Second'
    (d / 'Cover.JPG').write_bytes(JPEG_BYTES)
    al = album(do_scan(client, root), 'Alpha', 'Second')
    assert al['has_cover'] is True
    r = client.post('/api/prepare-embed', json={'album_path': al['path'], 'disc_info': al['disc_info'],
                                                'source': 'embedded'})
    assert r.status_code == 200
    assert sorted(p.name for p in d.iterdir() if p.suffix.lower() in ('.jpg', '.png')) == ['Cover.JPG']

def test_old_cache_version_forces_full_rescan(client, make_library, fake):
    import json
    from art_manager import config
    root = make_library()
    froot = 'fake://' + str(root)
    do_scan(client, froot)
    with open(config.CACHE_FILE) as f:
        cache = json.load(f)
    cache['version'] = 1
    with open(config.CACHE_FILE, 'w') as f:
        json.dump(cache, f)
    from art_manager import server
    server._cached_lib = None
    assert client.get('/api/library').json == {}   # outdated cache is not served
    fake.calls.clear()
    do_scan(client, froot)
    assert len(fake.calls) == ndirs(root)          # and the next scan is a full one

# ── Reliability (ROADMAP 2) ───────────────────────────────────────────────────

def test_transient_error_is_retried(client, make_library, fake):
    root = make_library()
    target = f'fake://{root}/Alpha/Second'
    fake.fail[target] = fail_once(TimeoutError('simulated SMB timeout'))
    lib = do_scan(client, 'fake://' + str(root))
    assert fake.calls.count(target) == 2
    assert {al['name'] for al in artist(lib, 'Alpha')['albums']} == {'First', 'Second'}
    assert lib['errors'] == []

def test_persistent_error_reported_and_retried_next_scan(client, make_library, fake):
    root = make_library()
    bad = f'fake://{root}/Alpha'
    fake.fail[bad] = ConnectionResetError('simulated dropped connection')
    lib = do_scan(client, 'fake://' + str(root))
    assert [e['path'] for e in lib['errors']] == [bad]
    assert 'Alpha' not in names(lib)   # nothing known about it yet → left out, but reported
    del fake.fail[bad]
    lib = do_scan(client, 'fake://' + str(root))
    assert lib['errors'] == []
    assert {al['name'] for al in artist(lib, 'Alpha')['albums']} == {'First', 'Second'}

def test_error_keeps_known_album_and_marks_dirty(client, make_library, fake):
    root = make_library()
    froot = 'fake://' + str(root)
    do_scan(client, froot)
    bad = f'{froot}/Alpha/Second'
    fake.fail[bad] = OSError('io error')
    bump(root / 'Alpha')
    lib = do_scan(client, froot)
    assert [e['path'] for e in lib['errors']] == [bad]
    assert {al['name'] for al in artist(lib, 'Alpha')['albums']} == {'First', 'Second'}
    assert artist(lib, 'Alpha').get('dirty') is True
    # Nothing changes on disk, but the dirty artist is fully re-read next time
    del fake.fail[bad]
    fake.calls.clear()
    lib = do_scan(client, froot)
    assert bad in fake.calls
    assert 'dirty' not in artist(lib, 'Alpha')

def test_unreadable_root_is_an_error(client, tmp_path):
    r = client.get('/api/scan', query_string={'root': str(tmp_path / 'nope')})
    assert r.status_code in (400, 502) and 'error' in r.json

def test_missing_fingerprint_always_rescans(client, make_library, fake, monkeypatch):
    """Backends that can't fingerprint folders (e.g. WebDAV without ETags) must never serve stale data."""
    from art_manager import storage
    real = fake.scandir
    def no_fp(path):
        return [storage.Entry(e.name, e.is_dir, None if e.is_dir else e.mtime, e.size) for e in real(path)]
    monkeypatch.setattr(fake, 'scandir', no_fp)
    root = make_library()
    froot = 'fake://' + str(root)
    do_scan(client, froot)
    (root / 'Alpha' / 'Second' / 'cover.jpg').write_bytes(JPEG_BYTES)   # no mtime bump needed
    lib = do_scan(client, froot)
    assert album(lib, 'Alpha', 'Second')['has_cover'] is True

# ── Ignore list (ROADMAP 3) ───────────────────────────────────────────────────

def test_playlists_ignored_everywhere_including_old_cache(client, make_library):
    root = make_library()
    client.post('/api/config', json={'ignored_folders': []})
    assert '!Playlists' in names(do_scan(client, root))   # cache now contains it
    client.post('/api/config', json={'ignored_folders': ['!playlists']})   # case-insensitive
    assert '!Playlists' not in names(client.get('/api/library').json)      # filtered immediately
    assert '!Playlists' not in names(do_scan(client, root))
    client.post('/api/config', json={'ignored_folders': []})
    assert '!Playlists' in names(do_scan(client, root))   # removing it brings it back

def test_ignore_applies_to_albums_discs_and_globs(client, make_library, flac_bytes):
    root = make_library()
    add_flac(root / 'Alpha' / '@eaDir' / 'x.flac', flac_bytes)
    add_flac(root / '100% Orange Juice' / 'Multi' / 'CD 3 (bonus)' / '01.flac', flac_bytes)
    lib = do_scan(client, root)   # defaults
    assert '@eaDir' not in {al['name'] for al in artist(lib, 'Alpha')['albums']}
    assert len(album(lib, '100% Orange Juice', 'Multi')['disc_info']) == 3
    client.post('/api/config', json={'ignored_folders': ['*bonus*', '!Playlists']})
    lib = do_scan(client, root)
    assert len(album(lib, '100% Orange Juice', 'Multi')['disc_info']) == 2
    assert '!Playlists' not in names(lib)

def test_default_ignore_list(client):
    assert config.is_ignored('.AppleDouble') and config.is_ignored('#RECYCLE')
    assert config.is_ignored('System Volume Information') and config.is_ignored('$Recycle.Bin')
    assert not config.is_ignored('...And You Will Know Us by the Trail of Dead')
    assert config.is_ignored('!Playlists') and config.is_ignored('!playlists')
    assert client.get('/api/config').json['ignored_folders'] == config.DEFAULT_IGNORED

def test_folders_without_albums_are_skipped_and_reported(client, make_library):
    root = make_library()
    (root / 'Scans').mkdir()
    (root / 'Scans' / 'booklet.jpg').write_bytes(JPEG_BYTES)
    (root / 'Playlists').mkdir()
    (root / 'Playlists' / 'mix.m3u').write_text('#EXTM3U')
    lib = do_scan(client, root)
    assert 'Scans' not in names(lib) and 'Playlists' not in names(lib)
    assert lib['skipped'] == ['Playlists', 'Scans']

def test_old_saved_ignore_list_gets_playlists_once(client):
    import json
    with open(config.CFG_FILE, 'w') as f:
        json.dump({'ignored_folders': ['@eaDir', 'Junk']}, f)
    config.load_cfg()
    assert config.cfg['ignored_folders'] == ['@eaDir', 'Junk', '!Playlists']
    # removing it again is respected: the migration runs only once
    client.post('/api/config', json={'ignored_folders': ['@eaDir']})
    config.cfg.clear(); config.cfg.update(config.DEFAULTS)
    config.load_cfg()
    assert config.cfg['ignored_folders'] == ['@eaDir']

def test_portable_mode_uses_folder_next_to_app(tmp_path, monkeypatch):
    import sys
    portable = tmp_path / 'art-manager-data'
    portable.mkdir()
    monkeypatch.delenv('ARTMGR_DATA_DIR', raising=False)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'art-manager.exe'))
    assert config._default_data_dir() == str(portable)
