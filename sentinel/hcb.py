"""
hcb.py — AES-256-GCM encrypted .hcb (HyperCore Balancer) export format.

File format (all binary, concatenated):
  [4 bytes]  Magic: b'HCB1'
  [32 bytes] Salt (random, for HKDF key derivation)
  [12 bytes] IV / nonce (random, for AES-GCM)
  [N bytes]  AES-256-GCM ciphertext
  [16 bytes] AES-GCM authentication tag

The AES key is derived from the bearer_token using HKDF-SHA256:
  HKDF(ikm=bearer_token.encode(), salt=salt, info=b'hcb-export-v1', length=32)

The plaintext is the canonical JSON of the signed snapshot
(output of exporter.sign_snapshot — a UTF-8 JSON string).
"""
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
import os

MAGIC = b'HCB1'


def encrypt(plaintext_json: str, bearer_token: str) -> bytes:
    """
    Encrypt a JSON string into .hcb binary format.
    Returns the complete file bytes ready to write to disk or stream.
    """
    salt = os.urandom(32)
    iv = os.urandom(12)
    key = _derive_key(bearer_token, salt)
    aesgcm = AESGCM(key)
    # AESGCM.encrypt returns ciphertext + tag concatenated
    ct_and_tag = aesgcm.encrypt(iv, plaintext_json.encode('utf-8'), None)
    # split: last 16 bytes are the tag
    ciphertext = ct_and_tag[:-16]
    tag = ct_and_tag[-16:]
    return MAGIC + salt + iv + ciphertext + tag


def decrypt(file_bytes: bytes, bearer_token: str) -> str:
    """
    Decrypt .hcb file bytes. Returns the plaintext JSON string.
    Raises ValueError on bad magic, or cryptography.exceptions.InvalidTag on tamper.
    """
    if file_bytes[:4] != MAGIC:
        raise ValueError(f"Invalid .hcb file: bad magic bytes")
    salt = file_bytes[4:36]
    iv = file_bytes[36:48]
    ciphertext = file_bytes[48:-16]
    tag = file_bytes[-16:]
    key = _derive_key(bearer_token, salt)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, None)
    return plaintext.decode('utf-8')


def _derive_key(bearer_token: str, salt: bytes) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b'hcb-export-v1',
    )
    return hkdf.derive(bearer_token.encode('utf-8'))
