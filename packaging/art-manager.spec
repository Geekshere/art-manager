# PyInstaller spec: one self-contained "art-manager" program (Python and all
# dependencies included). Build from the repository root:
#     pip install -r requirements.txt pyinstaller
#     pyinstaller packaging/art-manager.spec
# Output: dist/art-manager (dist/art-manager.exe on Windows).
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

hidden = []
for pkg in ('art_manager', 'ddgs', 'smbclient', 'smbprotocol', 'spnego'):
    hidden += collect_submodules(pkg)

a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, 'art_manager', 'templates'), os.path.join('art_manager', 'templates'))],
    hiddenimports=hidden,
    excludes=['tkinter', 'pytest', 'wsgidav', 'cheroot'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='art-manager',
    console=True,          # a window that shows the address; close it to stop the app
    upx=False,
    strip=False,
)
