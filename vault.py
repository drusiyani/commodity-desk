"""
Encryption for Argon's private data (Claude's trades, positions, decisions and thinking, and the bots' trades).

GitHub Pages and this public repo can be read by anyone, so hiding private data in the browser isn't enough: it
must never be stored or published in plain text. Everything private is encrypted with a key made from the
ARGON_PASSWORD secret:

  key = PBKDF2-HMAC-SHA256(password, random salt, 600,000 rounds)     # slow on purpose, against password guessing
  file = AES-256-GCM(key, random 12-byte IV, JSON)                     # GCM also detects any tampering

The website does the same steps in the browser with the Web Crypto API, so the password never leaves the
visitor's device. An encrypted file is plain JSON:
  {"v": 1, "kdf": "PBKDF2-SHA256", "iter": 600000, "salt": base64, "iv": base64, "ct": base64 (ciphertext + tag)}
"""
import base64
import hashlib
import json
import os

ITER = 600_000
_keys = {}


class Locked(Exception):
    """The password is missing or wrong, or the file was changed."""


def password():
    pw = os.environ.get("ARGON_PASSWORD", "")
    if not pw:
        raise Locked("The ARGON_PASSWORD secret isn't set, so the private data can't be read or saved. Add it under "
                     "Settings -> Secrets and variables -> Actions (see the README).")
    return pw


def derive(pw, salt, iters=ITER):
    k = (pw, salt, iters)
    if k not in _keys:
        _keys[k] = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters, 32)
    return _keys[k]


def encrypt(obj, pw, salt=None, iters=ITER):
    """salt: the deployment's salt (see engine.salt), so one slow key derivation covers every file, and the website
    can keep the key for the browser session; a random one if not given. The IV is always fresh."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = salt or os.urandom(16)
    iv = os.urandom(12)  # never reused: a fresh random IV for every file
    ct = AESGCM(derive(pw, salt, iters)).encrypt(iv, json.dumps(obj, separators=(",", ":")).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": iters, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt(blob, pw):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        salt, iv, ct = (base64.b64decode(blob[k]) for k in ("salt", "iv", "ct"))
        data = AESGCM(derive(pw, salt, int(blob["iter"]))).decrypt(iv, ct, None)
    except (InvalidTag, KeyError, ValueError, TypeError) as e:
        raise Locked("Couldn't decrypt the private data: wrong ARGON_PASSWORD, or the file was changed.") from e
    return json.loads(data)


def is_encrypted(obj):
    return isinstance(obj, dict) and obj.get("v") == 1 and "ct" in obj and "salt" in obj
