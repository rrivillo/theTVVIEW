"""Tests de theTVVIEW.

Fuerza el SecretStore en memoria para que la suite nunca toque el keyring
real del SO (GNOME Keyring / DPAPI / Keychain).
"""

import os

os.environ.setdefault("THETVVIEW_SECRET_STORE", "memory")
