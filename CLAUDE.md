# CLAUDE.md — Art Manager

Local Flask + vanilla-JS web app for managing artist images, `cover.jpg` files
and embedded FLAC artwork across a music library (local, SMB or WebDAV). Read
this first, then [ROADMAP.md](ROADMAP.md).

## Working agreement

- Run the app and the tests yourself and verify before saying something works.
  Never claim a fix without exercising it.
- Explain changes in plain language; keep commits small and clearly worded.
- Never commit `config.json`, `library_cache.json`, credentials, IPs, hostnames
  or anything identifying a real library. Use `nas.local`, `myuser`, etc. in
  examples and tests.
- Prefer the smallest change that solves the problem; this is a single-purpose
  tool, not a framework. Don't add dependencies without a reason.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # app + test dependencies
python app.py [path | smb://… | webdavs://…] [--port N]
pytest -q
```

Always use a venv (target platforms include distros with PEP 668 locked-down
system Python). `pip install .` installs the `art-manager` command instead.

## Layout

```
app.py                     shim: `python app.py` == `art-manager`
art_manager/
  config.py                cfg dict, config.json load/save/migration, env overrides, ignore list
  storage.py               backends (local / SMB / WebDAV) + dispatch helpers
  scan.py                  library scan, incremental fingerprint check, list_dir()
  artwork.py               find/write cover & artist images, embed into FLACs
  search.py                image sources + Provider registry + run_providers()
  server.py                Flask routes, path confinement, auth, embed jobs (SSE), CLI main()
  templates/index.html     the whole frontend: one SPA, vanilla JS, no build step
tests/                     pytest; tests/ui/ui_tests.js is driven by tests/test_ui.py
```

`config.cfg` is the single live settings dict: mutate it, never rebind it
(every module imports the same object).

### Data model (what `/api/scan` returns and the client holds as `lib`)

```
{ root, scanned_at,
  errors: [{ path, message }],          # folders that couldn't be read this scan
  artists: [{ name, path, has_img, img_path, dirty?,
              albums: [{ name, path, is_multi_disc, total_tracks,
                         has_cover, cover_path, cover_in_root,
                         disc_info: [{ name, path, flac_count, has_cover, cover_path }] }] }] }
```

The cached copy on disk also has `version` (`scan.SCHEMA_VERSION`) and
per-artist `fp` and `tree` (fingerprints, see below); `scan.client_view()`
strips the latter before sending. A cache with another `version` is neither
served by `/api/library` nor used for an incremental scan (bump the version
whenever the meaning of a cached field changes). `dirty: true` = this artist's
scan hit an error; it is never trusted from cache next time.

**The cover rule (owner's requirement):** an album is done (`has_cover`) only if
`cover.jpg` (case-insensitive) exists in the album folder (`cover_in_root`) AND,
for multi-disc albums, in every disc folder (`disc_info[].has_cover`). Embedded
art and other names (`folder.jpg`, `cover.png`, …) don't count; `cover_path` is
just the best image to *show*. The wizard, artist-list counts and Missing
report all follow `has_cover`, so embedded-only albums stay in the wizard (the
owner wants that). Every embed writes the same JPEG (`artwork.to_jpeg`) as
`cover.jpg` into the album folder and every disc folder (`write_covers`,
reusing an existing `Cover.JPG`-style name) and embeds it into every track.

### Storage layer

All file access goes through `storage.scandir / stat / open_file / join /
basename / isdir / isfile / read_bytes / write_bytes`, which dispatch on the
path's scheme: plain path or `file://` → local; `smb://host[:port]/share/…` →
SMB (smbprotocol); `webdav://`, `webdavs://`, `http(s)://` → WebDAV (plain
`requests`). Paths stay human-readable strings (names NOT percent-encoded)
everywhere and are converted to UNC paths / encoded URLs only inside the
backend. Credentials: `cfg['storage_username' / 'storage_password']` (+
`smb_port`), never in the URL; `config._migrate()` upgrades older configs
(`smb_username`/`smb_password`, credentials in the URL, UNC + `use_smb`).

`scandir()` returns `Entry(name, is_dir, mtime, size, etag)` in ONE round-trip;
`Entry.fingerprint` is the ETag, else the mtime, else `None` ("can't tell").
WebDAV: listing = one `PROPFIND Depth:1`; `'rb'` is a lazy HTTP Range reader;
`'rb+'`/`'wb'` edit in memory and upload on close via temp name + `MOVE` +
size check. Collection mtimes are not trusted on WebDAV (many servers never
update them) — only ETags.

### Incremental scan

Per artist the cache stores `fp` (from the root listing) and
`tree = {subdir: [fp, {subsubdir: fp} | None]}`. An artist is reused only if
its fp, every subfolder's fp and — for subfolders without tracks directly
inside (multi-disc albums, not-yet-albums) — every sub-subfolder's fp match.
Cost: one listing per artist + one per such subfolder; a cold scan is exactly
one listing per directory (asserted by tests). Any `None` fingerprint → rescan.
`/api/scan?full=1` (the artist list's **↺ Full rescan**) ignores the cache.

Errors: unreadable root → scan fails (502). Unreadable artist/album/disc →
`errors`, last known version kept if any, artist marked `dirty`. `list_dir()`
retries transient errors (`storage.is_permanent()` decides) and raises.

### Ignore list

`cfg['ignored_folders']` (`None` = `DEFAULT_IGNORED`): case-insensitive
`fnmatch` patterns, applied by `list_dir()` at every level and by
`apply_ignore()` on `/api/library` so a change applies to a cached library
immediately.

### Search providers

Each source is `fn(*args, page) -> [result]` and **raises** on failure,
registered as a `Provider` (key, label, kind `album|artist`, phase
`fast|slow`, `web` flag, `enabled(cfg)`, timeout) in `PROVIDERS`.
`run_providers()` gives each its own deadline on a shared pool and returns
`(results, sources)` with `status: ok|unavailable|disabled`; the UI renders the
chips from it. Web results are relevance-filtered and always last. Web search
= DuckDuckGo + Bing via the unofficial `ddgs` package in parallel (DDG answers
datacenter IPs with a 202 challenge). Discogs needs a token (unauthenticated
responses have empty `cover_image`).

### Embed flow

`POST /api/prepare-embed` converts the image to JPEG, writes `cover.jpg` into
the album folder and every disc folder, and creates a job →
`GET /api/embed/<job_id>` streams per-track progress over SSE (a job runs
once) → `artwork.embed_into()` per FLAC. Sources: `url`, `cover_file`,
`embedded`, `upload` (browser `data:` URL, JPEG/PNG only).

### Server safety

Binds 127.0.0.1 by default; when loopback-only, requests with a non-loopback
`Host` header get 403 (DNS rebinding). Optional `--password` basic auth. Every
route taking a path checks `_in_library()` (inside the configured/scanned root,
no `..`). Secrets never leave the server (`public_cfg()`).

### Client routing / wizard

URLs are `/<encodeURIComponent(artist)>/<encodeURIComponent(album)>/`; Flask
serves `index.html` for any path (catch-all, because `%2F` arrives decoded) and
the client resolves names against `lib`. The wizard is a **client-side filter
over `lib`** — no server call. The owner explicitly asked for this; do not
reintroduce a server-side "re-check" pass.

## Hard-won gotchas (each one cost a debugging round — don't regress them)

1. **mutagen on a shared file handle:** `embed_into` must `f.seek(0)` before
   `audio.save(f)`, or every embed fails with "file said 4 bytes, read 0 bytes".
2. **`smbclient.register_session` has an unguarded check-then-set race.**
   First registration per server is serialized in `SMBStorage._prep`. Keep SMB
   thread pools ≤ 8.
3. **smbclient pools connections by `server:port`.** Pass `port=` to EVERY
   smbclient call, or it silently opens a new session on 445 (the old code's
   "SMB port" setting never worked because of this).
4. **Never `entry.stat()` an smbclient scandir entry** — it's a network
   round-trip and drops the port. Use `entry.smb_info`.
5. **Never `urlsplit()` a full library path.** Folder names contain `#`, `?`,
   `%`. Parse scheme/host by hand (see `SMBStorage._split`, `WebDAVStorage._url`).
6. **Never build URL slugs with a regex that strips symbols** (an artist named
   `!!!` became an empty slug). Use `encodeURIComponent` on exact names.
7. **A folder's fingerprint does not change when something changes in a
   subfolder.** See *Incremental scan*; servers vary, `None` means rescan.
8. **Stale async responses.** Anything that writes shared UI state after an
   `await` must check a generation counter (`stepGen`, `artistSearchGen`,
   `albumSearchGen`) and drop stale responses. Code that finishes later
   (embed, save artist image) captures its target `album`/`artist` object up
   front — never re-read `lib.artists[curAI]` after an await.
9. **`.hidden` is `display:none !important`.** Show an element by removing the
   class, not by setting `style.display` (the embed-done banner never appeared
   because of this).
10. **NTFS updates a folder's entry in its parent's index lazily**, so on
    Windows `DirEntry.stat()` can return a stale mtime for subfolders;
    `LocalStorage` stats folders directly there. SMB servers running on Windows
    may show the same staleness in directory listings — **↺ Full rescan**
    (`/api/scan?full=1`) is the escape hatch.
11. **smbd in tests:** on shutdown it SIGTERMs its whole process group. Start it
    with `start_new_session=True` **and** `--no-process-group`, or it kills
    pytest (and without `--no-process-group` it fails to `setsid`).

## Testing

```bash
pytest -q                                    # everything that can run here
ARTMGR_LIVE=1 pytest -q tests/test_live_search.py -s    # real search services
```

- `conftest.py` builds a fake library from generated FLACs (needs `ffmpeg`),
  gives an isolated Flask client (config/cache in tmp), and a `fake://` storage
  backend that counts listings and injects faults (`fake.fail[path] = exc`).
- `test_backends.py` runs the same library through local, a real `smbd`
  (random port; needs Samba + root or passwordless sudo) and `wsgidav`
  (no folder ETags → exercises the "no fingerprint" path). Missing pieces skip.
- `test_ui.py` starts `app.py` with `ARTMGR_DATA_DIR` in tmp and runs
  `tests/ui/ui_tests.js` (Node Playwright; skipped without node/playwright).
  Search endpoints are mocked with `page.route`.
- FLAC template: `ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 1 -c:a flac x.flac`
  (has an 8 KiB padding block mutagen can embed into).
