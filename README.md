# Art Manager

A small local web tool for curating the artwork in a FLAC music library:
artist images, `cover.jpg` files, and the picture embedded in every track.
The library can be a local folder, an SMB share or a WebDAV server (Nextcloud,
ownCloud, most NAS boxes) — no drive mapping needed.

It only ever touches artwork — never tags, genres or filenames.

## Features

- Browse the library artist by artist; see the current `cover.jpg` and the art
  embedded in the tracks side by side, per disc
- Pick art from search results, a pasted URL, an uploaded file, the art already
  embedded in the tracks, or the existing `cover.jpg`
- Saves `cover.jpg` / `artist.jpg` and embeds the picture into every track,
  with a live progress bar
- **Start wizard** walks only the artists without `artist.jpg` and the albums
  without a cover, then returns to the artist list
- Fast incremental rescans: **Update Library** only re-reads folders that
  changed (**↺ Full rescan** re-reads everything)
- Missing-artwork report and an ignore list (`!Playlists`, NAS junk such as
  `@eaDir` or `#recycle`)
- Folders that can't be read (a flaky share) are reported and retried next time
  instead of silently disappearing

## Install and run

Python 3.10+ in a virtual environment (many Linux distributions block
system-wide `pip`):

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install .                      # or: pip install -r requirements.txt
art-manager /path/to/Music         # from a checkout, `python app.py …` works too
```

Then open <http://localhost:5000>. With no argument it uses the library saved
in Settings.

```bash
art-manager smb://nas.local/Media/Music --user myuser
art-manager webdavs://cloud.example.com/remote.php/dav/files/myuser/Music --user myuser
art-manager --host 0.0.0.0 --password 'choose-one'    # reachable from other devices
```

### Docker

```bash
docker build -t art-manager .
docker run -p 5000:5000 -v /path/to/Music:/music -v art-manager-data:/data \
           -e ARTMGR_PASSWORD=choose-one art-manager /music
```

See `compose.example.yaml`. The container listens on all interfaces, so set a
password; run it as the user that owns your music files.

## Library layout

```
Music/
  Artist/
    artist.jpg                (or artist.png)
    Album/
      cover.jpg               (or cover.png, folder.jpg, folder.png)
      01 - Track.flac
    Multi-Disc Album/
      CD 1/
        cover.jpg
        01 - Track.flac
      CD 2/
        ...
```

## Network libraries

Set **Library root** (Settings, or the command line) to:

| Library | Root |
|---|---|
| Local folder | `/mnt/music`, `D:\Music`, `file:///mnt/music` |
| SMB / Windows share | `smb://nas.local/Media/Music` (port `smb://host:1445/...` or in Settings) |
| WebDAV | `webdavs://host/path` (HTTPS) or `webdav://host/path` (HTTP); the plain `https://` URL works too |

Username and password fields appear in Settings for SMB and WebDAV; **Test
connection** checks them. Never put the password in the URL.

Over WebDAV the app reads only the start of each track to show its artwork, but
embedding has to download the track, edit it and upload it again (to a
temporary name, then moved into place and size-checked) — expect it to be
slower than SMB, which edits files in place. Servers that publish folder ETags
(Nextcloud, ownCloud) get fast incremental rescans; others are fully re-read on
every **Update Library**.

## Settings and secrets

Settings are saved to `config.json` (readable by your user only) next to
`app.py` in a checkout, or in `~/.config/art-manager/` (`%APPDATA%\art-manager`
on Windows) when installed; `ARTMGR_DATA_DIR` overrides the location. See
`config.example.json`.

Secrets can come from environment variables instead; they override
`config.json` and are never written to it:

| Variable | Setting |
|---|---|
| `ARTMGR_LIBRARY_ROOT` | library root |
| `ARTMGR_STORAGE_USERNAME` / `ARTMGR_STORAGE_PASSWORD` | SMB / WebDAV credentials |
| `ARTMGR_FANART_KEY` | Fanart.tv API key |
| `ARTMGR_DISCOGS_TOKEN` | Discogs personal token |
| `ARTMGR_PASSWORD` | same as `--password` |
| `ARTMGR_HOST` / `ARTMGR_PORT` | same as `--host` / `--port` |

## Artwork sources

| Source | Used for | Key |
|---|---|---|
| iTunes | album covers (up to 3000px) | none |
| Deezer | album covers, artist photos | none |
| Cover Art Archive (via MusicBrainz) | album covers | none |
| Wikipedia | artist photos | none |
| Fanart.tv | artist photos | free API key |
| Discogs | album covers | free personal token (Discogs only returns images to signed-in API users) |
| Web (DuckDuckGo + Bing images) | anything else | none — see below |

Each source is queried independently: one that is slow, down or rate-limited
shows as *unavailable* and never blocks the others.

**About web image search.** Neither DuckDuckGo nor Bing offers an official,
keyless image-search API. The app uses the [`ddgs`](https://pypi.org/project/ddgs/)
package, which talks to the same unofficial endpoints their websites use, so it
can break or be rate-limited at any time (DuckDuckGo often refuses cloud/datacenter
IPs, which is why Bing is queried alongside it). It is best effort: results are
filtered for relevance, shown after all other sources, and can be turned off in
Settings. For something sturdier, a provider for a keyed API (e.g. the Brave
Search API) can be added to `PROVIDERS` in `art_manager/search.py`.

## Security

- By default the server listens on `127.0.0.1` only and rejects requests whose
  `Host` header isn't `localhost`/`127.0.0.1` (DNS rebinding protection).
- `--host 0.0.0.0` makes it reachable from other devices; add `--password` (any
  username in the browser prompt). Without one, anyone who can reach the port
  can change your library. It is plain HTTP — don't expose it to the internet.
- Saved passwords and keys are never sent back to the browser.
- File requests are confined to the library folder.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

Tests need `ffmpeg` (to generate FLAC fixtures). Optional extras run
automatically when available: Node + Playwright for the browser tests, and
Samba (`smbd`, root or passwordless sudo) for the SMB integration tests. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [CLAUDE.md](CLAUDE.md) for how the code
is organised.

## License

[GPL-3.0-or-later](LICENSE). Copyright (C) 2026 Geekshere.
