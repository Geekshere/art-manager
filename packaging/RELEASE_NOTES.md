## Download and run — no Python needed

| System | File |
|---|---|
| Windows 10/11 (64-bit) | `art-manager-windows-x64.exe` |
| macOS (Apple Silicon: M1 and newer) | `art-manager-macos-arm64.zip` |
| Linux (64-bit) | `art-manager-linux-x64` |

Double-click it. A small window opens showing the address, and your browser
opens Art Manager. The first time, it asks where your music is (a folder on
this computer, a network share, or WebDAV); everything you enter is saved.
Close the small window (or click **⏻ Quit** in the app) to stop Art Manager.
Starting it again while it runs just opens your browser to it.

Your settings are kept between restarts and updates (Settings shows where).
For a portable setup, create a folder named `art-manager-data` next to the app.

If nothing seems to happen, see "If nothing seems to happen" in the README.

**First start warnings** (the apps are not code-signed):

- **Windows:** "Windows protected your PC" → *More info* → *Run anyway*.
- **macOS:** unzip, then *right-click* `art-manager-macos-arm64` → *Open* →
  *Open* (only needed once). If macOS says it is damaged, run
  `xattr -d com.apple.quarantine art-manager-macos-arm64` in Terminal.
- **Linux:** `chmod +x art-manager-linux-x64 && ./art-manager-linux-x64`.

Intel Macs and other systems: `uv tool install git+https://github.com/Geekshere/art-manager`
(see the README) — [uv](https://docs.astral.sh/uv/) takes care of Python for you.
