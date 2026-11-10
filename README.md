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
- Saves `cover.jpg` in the album folder **and in every disc folder** (CD 1,
  CD 2, …), saves `artist.jpg`, and embeds the same picture into every track,
  with a live progress bar. PNG/WebP artwork is converted to JPEG
- **Start wizard** walks only the artists without `artist.jpg` and the albums
  that are missing a `cover.jpg` anywhere, then returns to the artist list
- **Missing report with quick fixes:** one click gives every album and disc
  folder a `cover.jpg` from art that is already there (`folder.jpg`, CD 1's
  cover, the art embedded in the tracks) — never replacing an existing one
- **Check Files:** finds FLAC files that are cut off or damaged, files missing
  title/artist/album/track-number tags or an embedded cover, unreadable or
  low-resolution `cover.jpg` files, duplicate track numbers and more. It reads
  only the start and end of each file (about 2,000 files a second on a local
  disk) and remembers results, so a re-check only reads files that changed.
  Optional full decode if the `flac` program is installed
- Fast: a 1,500-artist / 90,000-track library scans in under a second locally;
  **Update Library** only re-reads folders that changed (**↺ Full rescan**
  re-reads everything)
- Skips folders that aren't artists: `!Playlists`, NAS junk (`@eaDir`,
  `#recycle`, …), your own ignore list, and any top-level folder with no albums
- Folders that can't be read (a flaky share) are reported and retried next time
  instead of silently disappearing
- Keyboard: **←** / **→** previous / next, **Esc** back to the artist list
- **⏻ Quit** in the top bar stops the app

## Install and run

### Easiest: download the app (no Python needed)

| System | Download |
|---|---|
| Windows 10/11 | [art-manager-windows-x64.exe](https://github.com/Geekshere/art-manager/releases/latest/download/art-manager-windows-x64.exe) |
| macOS (Apple Silicon) | [art-manager-macos-arm64.zip](https://github.com/Geekshere/art-manager/releases/latest/download/art-manager-macos-arm64.zip) |
| Linux (64-bit) | [art-manager-linux-x64](https://github.com/Geekshere/art-manager/releases/latest/download/art-manager-linux-x64) |

Double-click it. A small window shows the address and your browser opens Art
Manager; close the window to stop it. The apps aren't code-signed, so the first
start needs one extra click: Windows → *More info* → *Run anyway*; macOS →
right-click → *Open*; Linux → `chmod +x` first. All releases:
[Releases page](https://github.com/Geekshere/art-manager/releases).

### With one command (any system with internet access)

[uv](https://docs.astral.sh/uv/) installs Art Manager in its own isolated
environment — no venv to manage, and it fetches Python if you don't have it:

```bash
uv tool install git+https://github.com/Geekshere/art-manager
art-manager
```

Update later with `uv tool upgrade art-manager`. (`pipx install
git+https://github.com/Geekshere/art-manager` works the same way.)

### First start

The first time, Art Manager asks **where your music is**:

- **This computer** — pick the folder with **Browse…**
- **Network share (SMB)** — your NAS or a shared Windows folder: server name
  (or paste `\\nas\Media\Music`), share and folder, username and password
- **WebDAV** — Nextcloud, ownCloud or a NAS WebDAV server: address, username,
  password

**Test connection**, then **Save & scan library**. Everything is saved — next
time just start the app. Change it any time under **Settings → Change library…**.

Under **Settings → Access** you can let your phone or other computers on your
network use Art Manager and set an app password (both saved; they take effect
the next time you start it).

### If nothing seems to happen

- The app opens your browser by itself once it's ready (a second or two). If
  it doesn't, open the address shown in its window — normally
  <http://localhost:5000>. Starting the app again while it's running just
  opens the browser to the running one.
- **Windows:** a blue "Windows protected your PC" box → *More info* → *Run
  anyway*. Some antivirus tools hold new downloads for a scan first.
- **macOS:** "cannot be opened" → right-click the file → *Open* → *Open*.
- **Linux:** file managers often won't run programs on double-click; start it
  from a terminal: `chmod +x art-manager-linux-x64 && ./art-manager-linux-x64`.
- If it stops with an error, the window stays open and shows it; it is also
  saved in `art-manager.log` next to your settings (see below).

### Command line (optional)

Everything you pass is remembered, so you only ever pass it once:

```bash
art-manager /path/to/Music
art-manager smb://nas.local/Media/Music --user myuser --pass '...'
art-manager webdavs://cloud.example.com/remote.php/dav/files/myuser/Music --user myuser
art-manager --host 0.0.0.0 --password 'choose-one'   # this run only: other devices + password
art-manager --no-browser                             # don't open the browser
```

### Docker

```bash
docker build -t art-manager .
docker run -p 5000:5000 -v /path/to/Music:/music -v art-manager-data:/data \
           -e ARTMGR_PASSWORD=choose-one art-manager /music
```

See `compose.example.yaml`. The container listens on all interfaces, so set a
password; run it as the user that owns your music files.

### From source (for development)

```bash
git clone https://github.com/Geekshere/art-manager && cd art-manager
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python app.py
```

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

An album counts as done only when **`cover.jpg`** (any capitalisation) is in
the album folder and, for a multi-disc album, in every disc folder that holds
tracks. Art that is only embedded in the tracks doesn't count, and neither do
other file names such as `folder.jpg` or `cover.png` — they are shown as the
current cover, and **Keep current cover** turns them into `cover.jpg`
everywhere. The Missing report lists which folders are still missing one.

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

Settings are saved to `config.json` (readable by your user only): next to
`app.py` in a source checkout; otherwise in `%APPDATA%\art-manager` (Windows),
`~/Library/Application Support/Art Manager` (macOS) or `~/.config/art-manager`
(Linux) — they survive restarts and app updates, and **Settings** shows the
exact location. `ARTMGR_DATA_DIR` overrides it. **Portable mode:** create a
folder named `art-manager-data` next to the downloaded app and everything is
kept there instead (handy on a USB stick). The app password is stored only as
a salted hash. Also kept there: the scan cache (instant start), the last
file-check report and its per-file results, and `art-manager.log`. See
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
| `ARTMGR_DEBUG=1` | log what each search source returned (troubleshooting) |

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
- **Settings → Access → Other devices** (or `--host 0.0.0.0`) makes it reachable
  from other devices; set an **app password** there (or `--password`; any
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
