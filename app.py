#!/usr/bin/env python3
"""
Art Manager — run from a source checkout.

  python app.py                              # uses config.json, auto-scans the saved path
  python app.py /path/to/Music
  python app.py smb://nas.local/Media/Music --user myuser
  python app.py webdavs://cloud.example.com/remote.php/dav/files/myuser/Music
  python app.py --host 0.0.0.0 --password 'choose-one'

Installed with pip, the same thing is the `art-manager` command.
"""
from art_manager.server import main

if __name__ == '__main__':
    main()
