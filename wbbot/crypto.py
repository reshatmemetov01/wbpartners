import hashlib
import os
from pathlib import Path
from typing import Mapping

from cryptography.fernet import Fernet


class Crypto:
    """Шифрование токенов WB в базе (Fernet)."""

    def __init__(self, key: str | bytes):
        self._f = Fernet(key)

    def encrypt(self, text: str) -> str:
        return self._f.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._f.decrypt(token.encode()).decode()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load_crypto(env: Mapping[str, str], db_path: str) -> Crypto:
    """Ключ из ENCRYPTION_KEY, иначе из файла secret.key рядом с базой (создаётся при первом запуске)."""
    key = env.get("ENCRYPTION_KEY")
    if key:
        return Crypto(key)
    path = Path(db_path).with_name("secret.key")
    if path.exists():
        return Crypto(path.read_bytes().strip())
    key_bytes = Fernet.generate_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_bytes)
    return Crypto(key_bytes)
