# ROADMAP

Work queue for Art Manager, in priority order. Each item lists acceptance
criteria — an item is done when those are demonstrably true (tests or a scripted
run), not when the code looks right. Context and gotchas: [CLAUDE.md](CLAUDE.md).

Items 1–2 should land **before the repository is made public**.

---

## 1. Security hardening  *(P0)*

**✅ Done.** Also added: Host-header check against DNS rebinding when bound to loopback; `--password` / `ARTMGR_PASSWORD` basic auth.

Today: `app.run(host='0.0.0.0')`, no authentication, and `GET /api/config`
returns `smb_password` and `fanart_key` to any client that can reach the port.

- Default to `127.0.0.1`; add `--host` to opt in to other interfaces.
- `GET /api/config` must never return secrets: return `has_smb_password` /
  `has_fanart_key` booleans; the UI shows "••• saved" and only sends a new value
  when the user types one (empty = keep existing).
- Support credentials from environment variables as an alternative to
  `config.json`; create `config.json` with mode `0600`.
- If `--host` is non-loopback, print a clear warning (and consider an optional
  shared-secret/basic-auth flag).

**Acceptance:** with default settings, a request from another machine is
refused; `curl /api/config` contains no password or key; the Settings page still
saves and tests credentials; a unit test asserts secrets never appear in
`/api/config` responses.

## 2. Scan reliability  *(P0)*

**✅ Done.** Also: listing now uses `scandir` (one round-trip per directory), which was the main cost of item 6's performance point.

`_listdir` catches every exception and returns "empty". During a scan, one
transient SMB error makes an album silently disappear — and because the artist
is then cached with its folder mtime, the album stays invisible until that
folder changes.

- Let listing errors propagate to the scan; retry transient failures a couple of
  times.
- Never cache an artist whose scan had errors (mark it dirty so the next scan
  retries it).
- Return per-scan diagnostics (`errors: [{path, message}]`) and show them in the
  UI after **Update Library**, e.g. "3 folders could not be read".

**Acceptance:** a test with a backend that fails once then succeeds ends with
the album present; a backend that always fails for one artist yields an error
entry and that artist is retried on the next scan (not served from cache).

## 3. Ignore list, starting with `!Playlists`  *(requested)*

**✅ Done.** Defaults are editable entries (Settings → Ignored folders → Defaults). Uses `.[!.]*` rather than `.*` so artists like "...And You Will Know Us by the Trail of Dead" stay visible.

The owner keeps smart playlists in a folder named `!Playlists` at the library
root; it must never appear as an artist.

- New setting `ignored_folders` (list of case-insensitive names/glob patterns),
  editable in Settings, stored in `config.json`.
- Built-in defaults for common junk: names starting with `.`, `@eaDir`,
  `#recycle`, `$RECYCLE.BIN`, `System Volume Information`. The owner's own
  `!Playlists` goes in their local config, not hard-coded.
- Applies at every level (artist, album, disc folders).
- Ignored folders are absent from the artist list, "Jump to artist", counts, the
  wizard queue and the missing report.

**Acceptance:** with `["!Playlists"]` configured, a fixture library containing
that folder never lists it anywhere, including after a rescan from an older
cache that did contain it; removing it from the setting and rescanning brings it
back.

## 4. Stale search results when switching artist/album  *(requested — bug)*

**✅ Done.** Also fixed: finishing an embed after navigating away marked the wrong album as covered (or threw on an artist step).

Symptom: after moving to another artist (or album), the grid sometimes shows
results for the previous one until "Search" is clicked manually.

**Root cause (found, not a cache):** `searchArtistImg` / `searchAlbumArt` are
async and write into the *global* `allArtistResults` / `allAlbumResults`.
`loadArtistStep` / `loadAlbumStep` reset those arrays and start a new search,
but nothing cancels the previous request, so when the old response finally
arrives it is merged into the new artist's list and rendered. The album search
is worse: its second ("slow", CAA/web) phase takes many seconds.

**Fix:** a monotonically increasing generation counter (or `AbortController`).
Capture it when a search starts; after every `await`, drop the response if it no
longer matches. "Load more" must reuse the current generation, not bump it.
Apply the same guard to anything else that writes to shared UI state after an
await. Also send `Cache-Control: no-store` on `/api/image/*` and `/api/search/*`
and make sure replaced images (new `cover.jpg`) bust the browser cache on the
album step the way the artist step already does (`&t=`).

**Acceptance:** a Playwright test with a deliberately slow mocked search
endpoint clicks artist A then artist B immediately; the grid only ever contains
B's results, and A's late response is ignored. Same test for album → album and
for the two-phase album search.

## 5. Replace SearXNG with a keyless public image-search source  *(requested)*

**✅ Done, with one finding:** live testing showed DuckDuckGo answers datacenter IPs with a 202 bot challenge and no results, so web search queries DuckDuckGo **and** Bing (both via `ddgs`) in parallel, labelled per engine. Re-run `ARTMGR_LIVE=1 pytest tests/test_live_search.py -s` from your home network to see whether DDG works there. Also found: Discogs no longer returns image URLs without a token — now opt-in with a free personal token.

The owner no longer runs SearXNG. Remove the SearXNG config, UI, `--searxng`
flag, `/api/test-searxng` and docs. Introduce a small **provider interface**
(`search(query, kind, page) -> [Result]`, plus a name and an enabled flag) so
sources are pluggable and one failing never breaks the others.

For general web image search, use DuckDuckGo images. Be honest in code and docs
about the trade-off: **DuckDuckGo has no official image API.** The usual route
is the `ddgs` Python package (successor of `duckduckgo_search` — confirm the
current name on PyPI), which uses an unofficial endpoint that can break or
rate-limit. Therefore:

- Make it a *best-effort* provider: short timeout, swallow-and-report failures,
  never block the official sources, show "web search unavailable" in the source
  chips instead of an error.
- Keep the existing relevance filter (`_filter_by_relevance`) on web results.
- Keep the official, stable sources: iTunes, Deezer, Cover Art Archive
  (MusicBrainz), Wikipedia, optional Fanart.tv key.
- Document alternatives for people who want something sturdier (user-supplied
  keys: Brave Search API, etc.) and leave the interface ready for them.
- **Verify live before committing** that the DDG provider actually returns
  images from the dev machine; this has not been tested.

**Acceptance:** no mention of SearXNG remains in code, UI, README or config; with
the web provider mocked to raise/time out, album and artist search still return
the other sources within normal latency; a live smoke test (marked so CI can
skip it) shows real DDG results for a known artist.

## 6. General-purpose storage: local, SMB, WebDAV  *(requested)*

**✅ Done.** `art_manager/storage.py`; tested against a real `smbd` and `wsgidav`. Found and fixed on the way: the SMB port setting never took effect, and folder names containing `#`/`?` broke SMB paths. Directory fingerprints = ETag or mtime; WebDAV collection mtimes are deliberately not trusted.

Today only local paths and SMB work, via `fs_*` functions that dispatch on an
`smb://` prefix. Make storage a proper abstraction.

**Interface** (one class per backend, selected by the library root's scheme):

```
scandir(path) -> [Entry(name, is_dir, mtime|None, size|None)]   # ONE round-trip
open(path, mode)  /  read_bytes / write_bytes                    # see below
join, basename, dirname
fingerprint(path) -> str | None     # mtime or etag; None = can't tell, always rescan
exists / mkdir(optional)
```

- **Local**: plain paths and `file://`; Windows, Linux, macOS.
- **SMB**: `smb://host[:port]/share/path` (current behaviour, keep).
- **WebDAV**: `webdav://` / `webdavs://` (map to http/https) or an explicit
  type setting. Prefer raw `requests` with `PROPFIND Depth:1` — one call returns
  type, mtime and size for every entry and avoids a heavy dependency.
- Credentials: generalize `smb_username` / `smb_password` to backend-neutral
  fields; migrate old configs automatically; credentials never go in the URL.
- **Writes differ.** SMB and local support in-place random access (`rb+`).
  WebDAV does not: embedding into a FLAC means download → edit with mutagen →
  upload via a temporary name then `MOVE` (overwrite), then verify size. Hide this
  behind the backend so `embed_into` doesn't care.
- **Incremental scan** must use `fingerprint()`, not raw mtime. Many WebDAV
  servers don't update collection mtimes; Nextcloud-style ETags are reliable.
  When a backend returns `None`, rescan that directory every time.
- **Performance (do this as part of the work):** replace `_listdir`'s
  per-entry `fs_isfile` + `fs_isdir` (two round-trips per entry) with a single
  `scandir` per directory. This is the main reason a cold SMB scan takes
  minutes. Keep thread pools small (SMB ≤ 8).
- Settings UI: show only the fields relevant to the detected scheme; "Test
  connection" works for every backend.

**Acceptance:** the same fixture library scans identically through Local, SMB
(Samba in Docker) and WebDAV (e.g. `wsgidav` in a test process); embedding works
through all three and the picture is present afterward; the number of network
calls for a cold scan is O(directories), not O(entries) (assert with a counting
fake backend); a test covers a WebDAV server that returns no usable mtimes.

## 7. Publish to GitHub  *(requested)*

**✅ Done:** https://github.com/Geekshere/art-manager (public, GPL-3.0-or-later).

- Fresh `git init` with a single clean initial commit — do **not** carry over
  any history, and never add `config.json` or `library_cache.json`.
- Before pushing, run a secret/PII scan over the whole tree and the staged diff
  (e.g. `gitleaks` or `git grep` for IPv4 addresses, `@`, API-key patterns,
  usernames, real hostnames, library/artist names from real data). Screenshots
  must not show a real library.
- Create the repo with `gh` (needs `gh auth login` on the owner's machine).
  **Ask the owner** for the repository name, visibility, and license before
  creating anything (default suggestion: GPL-3.0-or-later; the copyright holder
  name must come from the owner).
- Add `LICENSE`, trim README to match reality, add a short CONTRIBUTING note and
  GitHub Actions running the tests from item 8.

**Acceptance:** `git ls-files` contains no secret/ignored files; the scan finds
nothing; the repo's first CI run is green.

## 8. Tests and CI  *(P2, but do it alongside items 2–6)*

**✅ Done:** pytest (scan, incremental cases, errors/retries, every embed
source, secrets, ignore list, URL round-trips, providers, storage), Playwright
UI tests, SMB + WebDAV integration tests, GitHub Actions on Linux (everything)
and Windows (portable subset) plus a Docker build/smoke test.

## 9. Packaging and cleanup  *(P2)*

**✅ Done:** `art_manager/` package, `pyproject.toml` (`art-manager` command), Dockerfile + `compose.example.yaml`, dead code removed.

- `pyproject.toml` with an entry point; optional `Dockerfile`/compose snippet
  (mount the library, expose the port) for homelab use.
- Remove dead code: server-side `slugify` / `slug` fields, unused imports, the
  stale `force` query parameter if still unused.
- Consider splitting `app.py` into modules (`storage/`, `scan.py`, `search/`,
  `embed.py`, `server.py`) once items 5 and 6 are in; don't do it earlier.

---

## Decided / already done (don't redo)

SMB via `smb://` prefix; incremental two-level mtime scan; wizard as a
client-side filter with return-to-list at the end; artist/album upload from the
browser; fixed embed (`seek(0)`); `!!!`-safe URLs; always-visible **Update
Library** button with "scanned Xm ago"; config migration of credentials out of
the URL.

## Open questions for the owner

1. Multiple libraries / storage roots? The storage layer would allow it; not scoped.

## Decided by the owner

- Albums whose art is only embedded stay in the wizard.
- Every album folder **and every disc folder** (CD 1, CD 2, …) must contain a
  `cover.jpg`; only that name counts. Embeds write it everywhere, as JPEG.

## Ideas (not scheduled)

- A keyed web-image provider (e.g. Brave Search API) for people who want
  something sturdier than the unofficial DuckDuckGo/Bing endpoints.
- Resize huge downloaded images before embedding (optional max size; conversion to JPEG is already done).
- Serve with a production WSGI server in the Docker image instead of Flask's.
