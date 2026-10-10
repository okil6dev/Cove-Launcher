"""
Encrypted secrets vault for Cove Launcher.

Threat model / design
---------------------
Secrets (Microsoft refresh tokens, API keys, Azure client id) are protected
by two independent layers:

1. **Windows DPAPI** (``CryptProtectData``, user scope) -- binds ciphertext
   to the current Windows user account. No key material exists on disk.
2. **AES-256-GCM** -- the actual payload encryption, using a random 32-byte
   master key that is itself stored only DPAPI-wrapped. GCM provides
   authenticated encryption: any tampering with the file is detected.

Neither layer is bypassable alone: a stolen ``vault.dat`` cannot be decrypted
without running as the same Windows user, and a DPAPI oracle alone does not
help without the wrapped key... which is in the same file but still requires
DPAPI. For protection beyond the Windows login an optional passphrase mode
(PBKDF2 -> AES-GCM) can be enabled later.

Secrets are never logged and only held in memory transiently.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .constants import VAULT_PATH

_VAULT_FORMAT_VERSION = 2
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class VaultError(Exception):
    """Raised when the vault cannot be read or written."""


# ---------------------------------------------------------------------------
# Windows DPAPI (ctypes, no external dependency)
# ---------------------------------------------------------------------------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _make_blob(data: bytes) -> _DATA_BLOB:
    buffer = ctypes.create_string_buffer(data, len(data))
    ptr = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    return _DATA_BLOB(len(data), ptr)


def dpapi_protect(data: bytes) -> bytes:
    """Encrypt *data* with the current user's DPAPI master key."""
    if not ctypes.windll.crypt32.CryptProtectData:
        raise VaultError("CryptProtectData unavailable")

    in_blob = _make_blob(data)
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),      # pDataIn
        "Cove vault",            # szDataDescr
        None,                       # pOptionalEntropy
        None,                       # pvReserved
        None,                       # pPromptStruct
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),     # pDataOut
    )
    if not ok:
        raise VaultError("CryptProtectData failed")
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return raw


def dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt data previously sealed by :func:`dpapi_protect`."""
    in_blob = _make_blob(data)
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise VaultError(
            "CryptUnprotectData failed (vault was created by a different "
            "Windows user?)"
        )
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)

# ---------------------------------------------------------------------------
# AES-256-GCM helpers
# ---------------------------------------------------------------------------
def _aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _aes_decrypt(key: bytes, sealed: bytes) -> bytes:
    nonce, ciphertext = sealed[:12], sealed[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None)

# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


class Vault:
    """
    Persistent encrypted key-value store.

    File layout (JSON)::

        {
          "format": 2,
          "wrapped_key": "<DPAPI(AES-256 master key), base64>",
          "payload": "<nonce || AES-GCM(json secrets), base64>"
        }

    Usage::

        vault = Vault()
        data = vault.read()            # {} on first run
        data["my_secret"] = "..."
        vault.write(data)
    """

    def __init__(self, path: str = VAULT_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._key: bytes | None = None

    # -- internals -----------------------------------------------------------
    def _ensure_dir(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)

    def _master_key(self) -> bytes:
        """Load (or first-run create) the AES master key, DPAPI-wrapped."""
        if self._key is not None:
            return self._key

        self._ensure_dir()
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as fh:
                envelope = json.load(fh)
            try:
                self._key = dpapi_unprotect(_unb64(envelope["wrapped_key"]))
            except (KeyError, VaultError) as exc:
                raise VaultError(
                    f"Cannot unlock vault at {self._path}: {exc}"
                ) from exc
        else:
            self._key = os.urandom(32)
            self._write_envelope({"secrets": {}})
        return self._key

    def _write_envelope(self, secrets: dict) -> None:
        assert self._key is not None
        payload = _aes_encrypt(self._key, json.dumps(secrets).encode("utf-8"))
        envelope = {
            "format": _VAULT_FORMAT_VERSION,
            "wrapped_key": _b64(dpapi_protect(self._key)),
            "payload": _b64(payload),
        }
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh)
        os.replace(tmp_path, self._path)  # atomic swap

    # -- public API ------------------------------------------------------------
    def read(self) -> dict:
        """Decrypt and return all stored secrets ({} when fresh)."""
        with self._lock:
            if not os.path.exists(self._path):
                return {}
            try:
                self._master_key()  # loads + unlocks
                with open(self._path, "r", encoding="utf-8") as fh:
                    envelope = json.load(fh)
                plaintext = _aes_decrypt(self._key or b"", _unb64(envelope["payload"]))
                return json.loads(plaintext.decode("utf-8"))
            except (OSError, ValueError, KeyError, InvalidTag) as exc:
                raise VaultError(f"Cannot read vault: {exc}") from exc

    def write(self, secrets: dict) -> None:
        """Atomically re-encrypt and store the full secrets mapping."""
        with self._lock:
            self._master_key()
            self._write_envelope(secrets)

    def get(self, key: str, default=None):
        return self.read().get(key, default)

    def set_value(self, key: str, value) -> None:
        data = self.read()
        data[key] = value
        self.write(data)


