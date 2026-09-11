"""Privacy and credential localisation (SRS chapter 18).

Core principle: credentials are stored locally and used locally only.

* Storage: Windows DPAPI when available (bound to the user account), otherwise a
  ChaCha20 + HMAC-SHA256 vault unlocked by a master password.  Both are
  implemented with the standard library only.
* Egress: an allow-list of official service hosts plus an offline switch, so a
  stray URL can never leak a credential to a third party.
* Audit: helper to prove that no secret text appears in configs, logs or
  exports.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import struct
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "CredentialVault",
    "EgressDenied",
    "EgressPolicy",
    "audit_for_secrets",
    "dpapi_available",
    "mask_secret",
]

_MASK32 = 0xFFFFFFFF
_VAULT_MAGIC = b"QSLV1"
_PBKDF2_ITERATIONS = 200_000


class EgressDenied(RuntimeError):
    """Raised when a request targets a host outside the allow-list."""


#: Official endpoints of the supported amateur-radio services.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "xmldata.qrz.com",
        "logbook.qrz.com",
        "www.qrz.com",
        "qrz.com",
        "www.hamqth.com",
        "hamqth.com",
        "clublog.org",
        "www.clublog.org",
        "www.eqsl.cc",
        "eqsl.cc",
        "lotw.arrl.org",
        "www.arrl.org",
    }
)


@dataclass(slots=True)
class EgressPolicy:
    """Decides whether an outbound URL is allowed (SRS PRIV-004/005)."""

    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS
    offline: bool = False

    def check(self, url: str) -> str:
        """Return the host name when allowed, else raise EgressDenied."""
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise EgressDenied(f"only https is allowed, got {parts.scheme or 'no scheme'!r}")
        host = (parts.hostname or "").lower()
        if not host:
            raise EgressDenied(f"no host in url: {url!r}")
        if self.offline:
            raise EgressDenied(f"offline mode blocks all network access ({host})")
        if host not in self.allowed_hosts:
            raise EgressDenied(f"host not in egress allow-list: {host}")
        return host

    def allows(self, url: str) -> bool:
        try:
            self.check(url)
        except EgressDenied:
            return False
        return True


def mask_secret(value: str, *, keep: int = 2) -> str:
    """Mask a credential for display (SRS PRIV-008)."""
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"


# --------------------------------------------------------------------------
# ChaCha20 (RFC 8439) - pure standard library, used only for tiny payloads
# --------------------------------------------------------------------------


def _rotl(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & _MASK32


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & _MASK32
    state[d] = _rotl(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & _MASK32
    state[b] = _rotl(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & _MASK32
    state[d] = _rotl(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & _MASK32
    state[b] = _rotl(state[b] ^ state[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    """One 64-byte ChaCha20 keystream block."""
    state = (
        list(struct.unpack("<4I", b"expand 32-byte k"))
        + list(struct.unpack("<8I", key))
        + [counter & _MASK32]
        + list(struct.unpack("<3I", nonce))
    )
    working = state[:]
    for _ in range(10):
        _quarter_round(working, 0, 4, 8, 12)
        _quarter_round(working, 1, 5, 9, 13)
        _quarter_round(working, 2, 6, 10, 14)
        _quarter_round(working, 3, 7, 11, 15)
        _quarter_round(working, 0, 5, 10, 15)
        _quarter_round(working, 1, 6, 11, 12)
        _quarter_round(working, 2, 7, 8, 13)
        _quarter_round(working, 3, 4, 9, 14)
    return struct.pack("<16I", *[(working[i] + state[i]) & _MASK32 for i in range(16)])


def chacha20_xor(key: bytes, nonce: bytes, data: bytes, counter: int = 1) -> bytes:
    out = bytearray(len(data))
    for offset in range(0, len(data), 64):
        block = chacha20_block(key, counter, nonce)
        counter += 1
        chunk = data[offset : offset + 64]
        for i, byte in enumerate(chunk):
            out[offset + i] = byte ^ block[i]
    return bytes(out)


def _derive_keys(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    material = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=64)
    return material[:32], material[32:]


def encrypt_blob(plaintext: bytes, password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> bytes:
    """Encrypt with ChaCha20 and authenticate with HMAC-SHA256 (encrypt-then-MAC)."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    enc_key, mac_key = _derive_keys(password, salt, iterations)
    ciphertext = chacha20_xor(enc_key, nonce, plaintext)
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return _VAULT_MAGIC + salt + nonce + tag + ciphertext


def decrypt_blob(blob: bytes, password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> bytes:
    """Reverse encrypt_blob; raises ValueError on tampering."""
    header = len(_VAULT_MAGIC) + 16 + 12 + 32
    if len(blob) < header or not blob.startswith(_VAULT_MAGIC):
        raise ValueError("not a qslcard vault blob")
    pos = len(_VAULT_MAGIC)
    salt, nonce, tag = blob[pos : pos + 16], blob[pos + 16 : pos + 28], blob[pos + 28 : pos + 60]
    ciphertext = blob[pos + 60 :]
    enc_key, mac_key = _derive_keys(password, salt, iterations)
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("vault integrity check failed (wrong master password or corrupted file)")
    return chacha20_xor(enc_key, nonce, ciphertext)


# --------------------------------------------------------------------------
# Windows DPAPI
# --------------------------------------------------------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_available() -> bool:
    return sys.platform.startswith("win")


def _dpapi(protect: bool, data: bytes) -> bytes:
    if not dpapi_available():
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    func.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_DataBlob),
    ]
    func.restype = ctypes.c_bool
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------
# Vault
# --------------------------------------------------------------------------


@dataclass(slots=True)
class CredentialVault:
    """Local-only credential store (SRS PRIV-001/002/009/010).

    With DPAPI the file is bound to the current Windows user account and needs
    no password.  Otherwise a master password is required and the payload is
    ChaCha20-encrypted.  Plaintext storage is never used.
    """

    path: str
    master_password: str | None = None
    iterations: int = _PBKDF2_ITERATIONS
    _cache: dict[str, str] = field(default_factory=dict, repr=False)

    def needs_master_password(self) -> bool:
        """True when the platform has no DPAPI and no password was supplied."""
        return self.master_password is None and not dpapi_available()

    def backend_label(self) -> str:
        """Non-raising description of where credentials live.

        backend raises when a master password is required but not yet supplied,
        which is exactly the state a privacy check runs in on Linux, so callers
        that only want to report the situation must use this instead.
        """
        if self.master_password is not None:
            return "chacha20"
        return "dpapi" if dpapi_available() else "master-password-required"

    @property
    def backend(self) -> str:
        if self.master_password is None and dpapi_available():
            return "dpapi"
        if self.master_password is None:
            raise ValueError("a master password is required on platforms without DPAPI")
        return "chacha20"

    # -- persistence -----------------------------------------------------
    def load(self) -> dict[str, str]:
        if self._cache:
            return self._cache
        self._cache = self._read_payload()
        return self._cache

    def _read_payload(self) -> dict[str, str]:
        file = Path(self.path)
        if not file.exists():
            return {}
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"vault file is not valid JSON: {self.path}") from exc
        backend = document.get("backend", "")
        payload = base64.b64decode(document.get("payload", ""))
        if backend == "dpapi":
            plaintext = _dpapi(False, payload)
        elif backend == "chacha20":
            if self.master_password is None:
                raise ValueError("this vault is password protected; supply a master password")
            plaintext = decrypt_blob(payload, self.master_password, iterations=self.iterations)
        else:
            raise ValueError(f"unknown vault backend: {backend!r}")
        parsed = json.loads(plaintext.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("vault payload must be a JSON object")
        return {str(k): str(v) for k, v in parsed.items()}

    def save(self) -> None:
        plaintext = json.dumps(self._cache, ensure_ascii=False).encode("utf-8")
        backend = self.backend
        if backend == "dpapi":
            payload = _dpapi(True, plaintext)
        else:
            payload = encrypt_blob(
                plaintext, self.master_password or "", iterations=self.iterations
            )
        document = {
            "version": 1,
            "backend": backend,
            "payload": base64.b64encode(payload).decode("ascii"),
        }
        file = Path(self.path)
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_suffix(file.suffix + ".tmp")
        tmp.write_text(json.dumps(document), encoding="utf-8")
        os.replace(tmp, file)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                file.chmod(0o600)

    # -- API -------------------------------------------------------------
    def set(self, name: str, secret: str) -> None:
        self.load()[name] = secret
        self.save()

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.load().get(name, default)

    def delete(self, name: str) -> bool:
        data = self.load()
        if name in data:
            del data[name]
            self.save()
            return True
        return False

    def clear(self) -> None:
        self._cache = {}
        file = Path(self.path)
        if file.exists():
            file.unlink()
        tmp = file.with_suffix(file.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()

    def names(self) -> list[str]:
        return sorted(self.load())

    def masked(self) -> dict[str, str]:
        return {name: mask_secret(value) for name, value in sorted(self.load().items())}

    def export_encrypted(self, destination: str, password: str) -> None:
        """Portable, password-protected credential bundle for migration."""
        payload = json.dumps(self.load(), ensure_ascii=False).encode("utf-8")
        Path(destination).write_bytes(encrypt_blob(payload, password, iterations=self.iterations))

    def import_encrypted(self, source: str, password: str, *, merge: bool = True) -> int:
        plaintext = decrypt_blob(Path(source).read_bytes(), password, iterations=self.iterations)
        incoming = json.loads(plaintext.decode("utf-8"))
        if not isinstance(incoming, dict):
            raise ValueError("credential bundle must contain a JSON object")
        data = self.load() if merge else {}
        data.update({str(k): str(v) for k, v in incoming.items()})
        self._cache = data
        self.save()
        return len(incoming)


# --------------------------------------------------------------------------
# Privacy self-check
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PrivacyFinding:
    severity: str
    path: str
    detail: str


def audit_for_secrets(
    paths: Iterable[str | os.PathLike[str]],
    secrets: Mapping[str, str] | Iterable[str],
    *,
    max_bytes: int = 4 * 1024 * 1024,
) -> list[PrivacyFinding]:
    """Scan files for credential plaintext (SRS AC-16)."""
    values = list(secrets.values()) if isinstance(secrets, Mapping) else list(secrets)
    values = [v for v in values if v and len(v) >= 4]
    findings: list[PrivacyFinding] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        if path.stat().st_size > max_bytes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for value in values:
            if value in text:
                findings.append(
                    PrivacyFinding(
                        "critical",
                        str(path),
                        f"contains credential material ({mask_secret(value)})",
                    )
                )
                break
    return findings
