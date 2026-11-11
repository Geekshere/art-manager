"""Tag normalisation, problem detection, writing, backups and undo."""
import shutil
import pytest
from mutagen.flac import FLAC, Picture
from art_manager import tags, config

def test_canonical_names():
    for k in ('album_artist', 'ALBUM ARTIST', 'AlbumArtist', 'album-artist', 'ALBUMARTIST'):
        assert tags.canonical(k) == 'ALBUMARTIST'
    assert tags.canonical('totaltracks') == 'TOTALTRACKS' and tags.canonical('YEAR') == 'DATE'   # companion: own field
    assert tags.canonical('MusicBrainz Album Id') == 'MUSICBRAINZ_ALBUMID'
    assert tags.canonical('my_custom_field') == 'MY_CUSTOM_FIELD'

def test_normalize_merges_variants_and_duplicates():
    pairs = [('TITLE', 'Song '), ('title', 'Song'), ('ALBUMARTIST', 'Band'), ('album_artist', 'Band'),
             ('ARTIST', 'A'), ('artist', 'B'), ('GENRE', 'Rock'), ('genre', 'rock'), ('COMMENT', ''),
             ('ALBUM', 'X'), ('album', 'Y')]
    out, notes = tags.normalize(pairs)
    assert out == {'TITLE': ['Song'], 'ALBUMARTIST': ['Band'], 'ARTIST': ['A', 'B'], 'GENRE': ['Rock'], 'ALBUM': ['X']}
    joined = ' | '.join(notes)
    assert 'merged ALBUMARTIST, album_artist into ALBUMARTIST' in joined and 'kept "X"' in joined

def test_canonical_spelling_wins_conflicts():
    out, _ = tags.normalize([('album_artist', 'Wrong'), ('ALBUMARTIST', 'Right')])
    assert out == {'ALBUMARTIST': ['Right']}
    out, _ = tags.normalize([('YEAR', '1994'), ('DATE', '1994-10-17')])
    assert out == {'DATE': ['1994-10-17']}

def test_problem_detection():
    codes = {c for _, c, _ in tags.tag_problems([
        ('ALBUMARTIST', 'Band'), ('album_artist', 'Band'), ('TITLE', 'CafÃ© del Mar'),
        ('TRACKNUMBER', 'A1'), ('DATE', '94'), ('ALBUM', ' X'), ('GENRE', 'Rock'), ('GENRE', 'Rock')])}
    assert {'tag_spellings', 'mojibake', 'bad_tracknumber', 'odd_date', 'tag_whitespace', 'dup_values'} <= codes
    assert tags.tag_problems([('TITLE', 'Café'), ('TRACKNUMBER', '3/12'), ('DATE', '1994-10-17')]) == []

def test_read_write_keeps_pictures_and_spellings(tmp_path, flac_bytes):
    p = tmp_path / 'x.flac'; p.write_bytes(flac_bytes)
    f = FLAC(str(p)); pic = Picture(); pic.type = 3; pic.mime = 'image/png'; pic.data = b'\x89PNG' + b'0' * 50
    f.add_picture(pic); f.save()
    tags.write(str(p), [('ALBUMARTIST', 'Band'), ('album_artist', 'Band'), ('TITLE', 'Ünïcødé')])
    assert tags.read_raw(str(p)) == [('ALBUMARTIST', 'Band'), ('album_artist', 'Band'), ('TITLE', 'Ünïcødé')]
    assert len(FLAC(str(p)).pictures) == 1
    tags.write(str(p), {'TITLE': ['One'], 'ARTIST': ['A', 'B']})
    assert tags.read_raw(str(p)) == [('TITLE', 'One'), ('ARTIST', 'A'), ('ARTIST', 'B')]

def test_apply_backup_and_undo(tmp_path, flac_bytes, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', str(tmp_path))
    p = tmp_path / 'x.flac'; p.write_bytes(flac_bytes)
    original = [('album_artist', 'Band'), ('ALBUMARTIST', 'Band'), ('title', 'Song')]
    tags.write(str(p), original)
    new, _ = tags.normalize(tags.read_raw(str(p)))
    bid, errors = tags.apply([{'path': str(p), 'tags': new}], label='Clean up')
    assert errors == [] and tags.read_raw(str(p)) == [('ALBUMARTIST', 'Band'), ('TITLE', 'Song')]
    assert tags.list_backups()[0]['id'] == bid
    tags.undo(bid)
    assert tags.read_raw(str(p)) == original                  # exactly as before, both spellings
    with pytest.raises(ValueError):
        tags.undo('../../etc/passwd')

def test_read_raw_after_id3(tmp_path, flac_bytes):
    p = tmp_path / 'x.flac'; p.write_bytes(flac_bytes)
    tags.write(str(p), {'TITLE': ['T']})
    p.write_bytes(b'ID3\x03\x00\x00\x00\x00\x00\x0a' + b'\x00' * 10 + p.read_bytes())
    assert tags.read_raw(str(p)) == [('TITLE', 'T')]

def test_mojibake_repaired_by_cleanup():
    assert tags.fix_mojibake('StrÃ¤ngers') == 'Strängers'
    assert tags.fix_mojibake('CafÃ© del Mar') == 'Café del Mar'
    assert tags.fix_mojibake('Björk') == 'Björk'                 # fine text untouched
    out, notes = tags.normalize([('TITLE', 'BeyoncÃ©')])
    assert out == {'TITLE': ['Beyoncé']} and any('repaired' in n for n in notes)


LIDARR = [('TRACKTOTAL', '12'), ('TOTALTRACKS', '12'), ('DISCTOTAL', '1'), ('TOTALDISCS', '1'),
          ('RELEASETYPE', 'album'), ('MUSICBRAINZ_ALBUMTYPE', 'album'),
          ('RELEASESTATUS', 'official'), ('MUSICBRAINZ_ALBUMSTATUS', 'official'),
          ('TITLE', 'Song'), ('ARTIST', 'Band'), ('ALBUM', 'Album'), ('TRACKNUMBER', '1')]

def test_lidarr_picard_companion_fields_are_not_problems():
    assert tags.tag_problems(LIDARR) == []
    clean, notes = tags.normalize(LIDARR)
    assert clean['TOTALTRACKS'] == ['12'] and clean['MUSICBRAINZ_ALBUMTYPE'] == ['album'] and notes == []

def test_companion_that_disagrees_is_reported_and_fixed():
    bad = [p if p[0] != 'TOTALTRACKS' else ('TOTALTRACKS', '11') for p in LIDARR]
    assert [c for _, c, _ in tags.tag_problems(bad)] == ['companion_mismatch']
    clean, notes = tags.normalize(bad)
    assert clean['TOTALTRACKS'] == ['12'] and notes

def test_saving_keeps_companions_in_step(tmp_path, flac_bytes=None):
    from mutagen.flac import FLAC
    import shutil, subprocess
    if not shutil.which('ffmpeg'):
        import pytest; pytest.skip('ffmpeg needed')
    p = tmp_path / 'a.flac'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=44100:cl=mono', '-t', '1',
                    '-c:a', 'flac', str(p)], check=True)
    tags.write(str(p), LIDARR)
    new, _ = tags.normalize(tags.read_raw(str(p)))
    new['TRACKTOTAL'] = ['13']
    del new['RELEASETYPE']
    tags.write(str(p), new)
    got = dict(tags.normalize(tags.read_raw(str(p)))[0])
    assert got['TOTALTRACKS'] == ['13'] and 'MUSICBRAINZ_ALBUMTYPE' not in got
