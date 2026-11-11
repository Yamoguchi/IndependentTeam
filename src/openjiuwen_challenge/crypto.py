"""Ed25519 raw signatures: explicitly supported by ssh-key-v1."""

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def create_key(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = Ed25519PrivateKey.generate()
    data = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    )
    public_path = path.with_suffix(path.suffix + ".pub")
    if public_path.exists():
        raise FileExistsError(f"Публичный ключ уже существует: {public_path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    path.with_suffix(path.suffix + ".pub").write_bytes(public + b"\n")
    return fingerprint(public.decode())


def load_key(path: Path):
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("Приватный ключ доступен другим пользователям; выполните chmod 600")
    key = serialization.load_ssh_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Нужен отдельный SSH Ed25519 ключ")
    return key


def public_key(key):
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


def fingerprint(public):
    blob = base64.b64decode(public.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def sign(key, message):
    return base64.b64encode(key.sign(message.encode("utf-8"))).decode("ascii")
