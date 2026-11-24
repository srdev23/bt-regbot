from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from sn_reg_bot.database.repository import WalletSecretRepository, WalletSecretRecord

logger = logging.getLogger(__name__)


def build_fernet(key_material: str) -> Fernet:
    digest = hashlib.sha256(key_material.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class WalletPasswordStore:
    """Encrypts wallet passwords at rest so sessions can be restored after restart."""

    def __init__(self, repo: WalletSecretRepository, fernet: Fernet | None) -> None:
        self._repo = repo
        self._fernet = fernet

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def _encrypt(self, user_id: int, wallet_name: str, password: str) -> str:
        assert self._fernet is not None
        payload = f"{user_id}\0{wallet_name}\0{password}".encode()
        return self._fernet.encrypt(payload).decode()

    def _decrypt(self, user_id: int, wallet_name: str, blob: str) -> str:
        assert self._fernet is not None
        try:
            raw = self._fernet.decrypt(blob.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored wallet secret is invalid or corrupted.") from exc
        parts = raw.split("\0", 2)
        if len(parts) != 3:
            raise ValueError("Stored wallet secret has unexpected format.")
        stored_uid, stored_name, password = parts
        if int(stored_uid) != user_id or stored_name != wallet_name:
            raise ValueError("Stored wallet secret does not match record.")
        return password

    async def save(self, user_id: int, wallet_name: str, password: str) -> None:
        if not self.enabled:
            return
        blob = self._encrypt(user_id, wallet_name, password)
        await self._repo.save(user_id, wallet_name, blob)
        logger.debug("Persisted wallet password user=%s wallet=%s", user_id, wallet_name)

    async def delete(self, user_id: int, wallet_name: str) -> None:
        await self._repo.delete(user_id, wallet_name)

    async def list_all(self) -> list[WalletSecretRecord]:
        return await self._repo.list_all()

    def decrypt_record(self, record: WalletSecretRecord) -> str:
        return self._decrypt(record.user_id, record.wallet_name, record.secret_blob)
