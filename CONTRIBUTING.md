# Contributing

Thanks for helping! This is a small, single-purpose tool — the bar for new
dependencies and features is "does it make curating artwork easier?".

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

You need `ffmpeg` on your PATH (tests generate FLAC files with it). These run
automatically when available and are skipped otherwise:

- browser tests: Node.js + `npm install --no-save playwright` and
  `npx playwright install chromium`
- SMB integration tests: Samba's `smbd`, plus root or passwordless `sudo`
- live search tests: `ARTMGR_LIVE=1 pytest tests/test_live_search.py -s`

CI runs everything on Linux and the portable subset on Windows.

## Guidelines

- Read [CLAUDE.md](CLAUDE.md) first: it explains the architecture and lists
  bugs that were hard to find — please don't reintroduce them.
- Every bug fix comes with a test that fails without the fix.
- All file access goes through `art_manager/storage.py`, so it works for local,
  SMB and WebDAV libraries alike.
- New image sources: add a function that raises on failure and register it as a
  `Provider` in `art_manager/search.py`.
- Never commit `config.json`, `library_cache.json`, credentials, real hostnames
  or anything from a real library (use `nas.local`, `myuser`, ...). That
  includes screenshots.
- Keep commits small, with messages that explain *why*.

By contributing you agree that your contribution is licensed under the
[GPL-3.0-or-later](LICENSE).
