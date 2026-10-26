"""Pairing and end-to-end encryption for the Car Review relay.

One random 32-byte secret (shown once as a QR code) is the only thing a laptop and phone
share. Everything else is derived from it with HKDF-SHA256, so the relay never sees it:

    box id      which box on the relay belongs to this user (one-way, safe to send)
    write key   proves to the relay that a write comes from this user
    encrypt key AES-256-GCM key; never leaves the user's devices
    name key    HMAC key for naming audio files, so the relay can't match clips across days

Encrypted file format: 12-byte random nonce + ciphertext + 16-byte tag, with the file's name
as associated data so the relay can't swap one file for another. The phone app implements the
same scheme with WebCrypto.

The box id and keys need only Python's standard library, so Anki itself can pair a phone
before the voice helper is installed. Encrypting and decrypting need `cryptography`, which
comes with the helper.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

SALT = b"car-review-v1"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def from_b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(secret: bytes, label: bytes) -> bytes:
    """HKDF-SHA256 (RFC 5869) with a 32-byte output: one extract and one expand step."""
    prk = hmac.new(SALT, secret, hashlib.sha256).digest()
    return hmac.new(prk, label + b"\x01", hashlib.sha256).digest()


def _aes(secret: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # only in the helper's Python

    return AESGCM(_derive(secret, b"encrypt"))


@dataclass(frozen=True)
class Pairing:
    secret: bytes

    @classmethod
    def new(cls) -> "Pairing":
        return cls(os.urandom(32))

    @classmethod
    def from_code(cls, code: str) -> "Pairing":
        return cls(from_b64url(code))

    @property
    def code(self) -> str:
        """The secret as text, for the QR code link."""
        return b64url(self.secret)

    @property
    def box_id(self) -> str:
        return _derive(self.secret, b"box").hex()

    @property
    def write_key(self) -> str:
        return b64url(_derive(self.secret, b"write"))

    def encrypt(self, name: str, data: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + _aes(self.secret).encrypt(nonce, data, name.encode())

    def decrypt(self, name: str, blob: bytes) -> bytes:
        return _aes(self.secret).decrypt(blob[:12], blob[12:], name.encode())

    def audio_name(self, fingerprint: str) -> str:
        """Relay file name for a clip, keyed so it reveals nothing about the card."""
        mac = hmac.new(_derive(self.secret, b"names"), fingerprint.encode(), hashlib.sha256)
        return "audio/" + mac.hexdigest()[:32]
