from __future__ import annotations

import logging
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError

from sn_reg_bot.wallet.discovery import WalletInfo, discover_wallets, wallet_exists

logger = logging.getLogger(__name__)


def _safe_extractall(zf: zipfile.ZipFile, dest: Path) -> None:
    dest_root = dest.resolve()
    for member in zf.namelist():
        target = (dest_root / member).resolve()
        if dest_root not in target.parents and target != dest_root:
            raise ValueError("Unsafe path in zip archive.")
    zf.extractall(dest_root)


@dataclass(frozen=True)
class WalletCreateResult:
    name: str
    coldkey_ss58: str
    mnemonic: str | None = None


@dataclass(frozen=True)
class HotkeyCreateResult:
    coldkey_name: str
    hotkey_name: str
    hotkey_ss58: str


class WalletManager:
    """User-scoped wallet filesystem operations."""

    def __init__(self, wallet_path: Path, user_id: int) -> None:
        self.wallet_path = wallet_path
        self.user_id = user_id
        self.wallet_path.mkdir(parents=True, exist_ok=True)

    def list_wallets(self) -> list[WalletInfo]:
        return discover_wallets(self.wallet_path)

    def get_wallet(self, name: str, hotkey: str | None = None) -> Wallet:
        hk = hotkey or "default"
        return Wallet(name=name, hotkey=hk, path=str(self.wallet_path))

    def coldkey_exists(self, name: str) -> bool:
        return wallet_exists(self.wallet_path, name)

    def hotkey_exists(self, coldkey_name: str, hotkey_name: str) -> bool:
        hotkey_file = self.wallet_path / coldkey_name / "hotkeys" / hotkey_name
        return hotkey_file.is_file()

    def coldkey_requires_password(self, name: str) -> bool:
        if not self.coldkey_exists(name):
            return True
        wallet = Wallet(name=name, hotkey="default", path=str(self.wallet_path))
        if not wallet.coldkey_file.exists_on_device():
            return False
        return wallet.coldkey_file.is_encrypted()

    def create_coldkey(
        self,
        name: str,
        password: str,
        *,
        n_words: int = 12,
        overwrite: bool = False,
    ) -> WalletCreateResult:
        if self.coldkey_exists(name) and not overwrite:
            raise FileExistsError(f"Coldkey '{name}' already exists.")

        wallet = Wallet(name=name, hotkey="default", path=str(self.wallet_path))
        wallet.create_new_coldkey(
            n_words=n_words,
            use_password=bool(password),
            overwrite=overwrite,
            coldkey_password=password or None,
            suppress=True,
        )
        ss58 = wallet.coldkeypub.ss58_address
        logger.info("Created coldkey user=%s name=%s ss58=%s", self.user_id, name, ss58)
        return WalletCreateResult(name=name, coldkey_ss58=ss58, mnemonic=None)

    def import_coldkey_from_mnemonic(
        self,
        name: str,
        mnemonic: str,
        password: str,
        *,
        overwrite: bool = False,
    ) -> WalletCreateResult:
        if self.coldkey_exists(name) and not overwrite:
            raise FileExistsError(f"Coldkey '{name}' already exists.")

        wallet = Wallet(name=name, hotkey="default", path=str(self.wallet_path))
        wallet.regenerate_coldkey(
            mnemonic=mnemonic.strip(),
            use_password=bool(password),
            overwrite=overwrite,
            coldkey_password=password or None,
            suppress=True,
        )
        ss58 = wallet.coldkeypub.ss58_address
        logger.info("Imported coldkey user=%s name=%s ss58=%s", self.user_id, name, ss58)
        return WalletCreateResult(name=name, coldkey_ss58=ss58)

    def create_hotkey(
        self,
        coldkey_name: str,
        hotkey_name: str,
        *,
        password: str | None = None,
        overwrite: bool = False,
    ) -> HotkeyCreateResult:
        if not self.coldkey_exists(coldkey_name):
            raise FileNotFoundError(f"Coldkey '{coldkey_name}' not found.")
        if self.hotkey_exists(coldkey_name, hotkey_name) and not overwrite:
            raise FileExistsError(
                f"Hotkey '{hotkey_name}' already exists on coldkey '{coldkey_name}'."
            )

        wallet = Wallet(name=coldkey_name, hotkey=hotkey_name, path=str(self.wallet_path))
        if password:
            wallet.get_coldkey(password=password)
        wallet.create_new_hotkey(
            use_password=False,
            overwrite=overwrite,
            suppress=True,
        )
        ss58 = wallet.hotkeypub.ss58_address
        logger.info(
            "Created hotkey user=%s coldkey=%s hotkey=%s ss58=%s",
            self.user_id,
            coldkey_name,
            hotkey_name,
            ss58,
        )
        return HotkeyCreateResult(
            coldkey_name=coldkey_name,
            hotkey_name=hotkey_name,
            hotkey_ss58=ss58,
        )

    def import_hotkey_from_mnemonic(
        self,
        coldkey_name: str,
        hotkey_name: str,
        mnemonic: str,
        *,
        overwrite: bool = False,
    ) -> HotkeyCreateResult:
        if not self.coldkey_exists(coldkey_name):
            raise FileNotFoundError(f"Coldkey '{coldkey_name}' not found.")
        if self.hotkey_exists(coldkey_name, hotkey_name) and not overwrite:
            raise FileExistsError(
                f"Hotkey '{hotkey_name}' already exists on coldkey '{coldkey_name}'."
            )

        wallet = Wallet(name=coldkey_name, hotkey=hotkey_name, path=str(self.wallet_path))
        wallet.regenerate_hotkey(
            mnemonic=mnemonic.strip(),
            use_password=False,
            overwrite=overwrite,
            suppress=True,
        )
        ss58 = wallet.hotkeypub.ss58_address
        logger.info(
            "Imported hotkey from mnemonic user=%s coldkey=%s hotkey=%s",
            self.user_id,
            coldkey_name,
            hotkey_name,
        )
        return HotkeyCreateResult(
            coldkey_name=coldkey_name,
            hotkey_name=hotkey_name,
            hotkey_ss58=ss58,
        )

    def import_hotkey_from_files(
        self,
        coldkey_name: str,
        hotkey_name: str,
        hotkey_file: Path,
        hotkey_pub_file: Path | None,
        *,
        overwrite: bool = False,
    ) -> HotkeyCreateResult:
        if not self.coldkey_exists(coldkey_name):
            raise FileNotFoundError(f"Coldkey '{coldkey_name}' not found.")
        if self.hotkey_exists(coldkey_name, hotkey_name) and not overwrite:
            raise FileExistsError(
                f"Hotkey '{hotkey_name}' already exists on coldkey '{coldkey_name}'."
            )

        dest_dir = self.wallet_path / coldkey_name / "hotkeys"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_hotkey = dest_dir / hotkey_name
        if dest_hotkey.exists() and not overwrite:
            raise FileExistsError(
                f"Hotkey '{hotkey_name}' already exists on coldkey '{coldkey_name}'."
            )

        shutil.copy2(hotkey_file, dest_hotkey)
        if hotkey_pub_file is not None and hotkey_pub_file.exists():
            shutil.copy2(hotkey_pub_file, dest_dir / f"{hotkey_name}pub.txt")

        wallet = Wallet(name=coldkey_name, hotkey=hotkey_name, path=str(self.wallet_path))
        ss58 = wallet.hotkeypub.ss58_address
        logger.info(
            "Imported hotkey from file user=%s coldkey=%s hotkey=%s",
            self.user_id,
            coldkey_name,
            hotkey_name,
        )
        return HotkeyCreateResult(
            coldkey_name=coldkey_name,
            hotkey_name=hotkey_name,
            hotkey_ss58=ss58,
        )

    def import_hotkey_from_zip(
        self,
        coldkey_name: str,
        hotkey_name: str,
        zip_path: Path,
        *,
        overwrite: bool = False,
    ) -> HotkeyCreateResult:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with zipfile.ZipFile(zip_path, "r") as zf:
                _safe_extractall(zf, tmp_path)

            hotkey_file, pub_file = self._find_hotkey_files_in_tree(tmp_path, hotkey_name)
            return self.import_hotkey_from_files(
                coldkey_name,
                hotkey_name,
                hotkey_file,
                pub_file,
                overwrite=overwrite,
            )

    @staticmethod
    def _find_hotkey_files_in_tree(root: Path, hotkey_name: str) -> tuple[Path, Path | None]:
        hotkey_file: Path | None = None
        pub_file: Path | None = None
        pub_name = f"{hotkey_name}pub.txt"

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.name == hotkey_name:
                hotkey_file = path
            elif path.name == pub_name:
                pub_file = path

        if hotkey_file is None:
            raise ValueError(
                f"Could not find hotkey file '{hotkey_name}' in the uploaded archive."
            )
        return hotkey_file, pub_file

    def import_wallet_from_directory(
        self,
        source_dir: Path,
        target_name: str,
        *,
        overwrite: bool = False,
    ) -> WalletCreateResult:
        if self.coldkey_exists(target_name) and not overwrite:
            raise FileExistsError(f"Coldkey '{target_name}' already exists.")

        coldkey = source_dir / "coldkey"
        coldkeypub = source_dir / "coldkeypub.txt"
        if not coldkey.exists() and not coldkeypub.exists():
            raise ValueError("Source directory is not a valid Bittensor wallet.")

        dest = self.wallet_path / target_name
        if dest.exists():
            if not overwrite:
                raise FileExistsError(f"Coldkey '{target_name}' already exists.")
            shutil.rmtree(dest)

        shutil.copytree(source_dir, dest)
        wallet = Wallet(name=target_name, hotkey="default", path=str(self.wallet_path))
        ss58 = wallet.coldkeypub.ss58_address
        logger.info("Imported wallet files user=%s name=%s ss58=%s", self.user_id, target_name, ss58)
        return WalletCreateResult(name=target_name, coldkey_ss58=ss58)

    def import_wallet_from_zip(
        self,
        zip_path: Path,
        target_name: str,
        *,
        overwrite: bool = False,
    ) -> WalletCreateResult:
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with zipfile.ZipFile(zip_path, "r") as zf:
                _safe_extractall(zf, tmp_path)

            candidates = [p for p in tmp_path.iterdir() if p.is_dir()]
            if len(candidates) == 1:
                source = candidates[0]
            else:
                if (tmp_path / "coldkey").exists() or (tmp_path / "coldkeypub.txt").exists():
                    source = tmp_path
                else:
                    raise ValueError(
                        "ZIP must contain a single wallet folder or coldkey files at root."
                    )
            return self.import_wallet_from_directory(source, target_name, overwrite=overwrite)

    def validate_wallet_access(self, coldkey_name: str) -> None:
        if not self.coldkey_exists(coldkey_name):
            raise PermissionError(f"Wallet '{coldkey_name}' not found for this user.")

    def get_hotkeys(self, coldkey_name: str) -> list[str]:
        self.validate_wallet_access(coldkey_name)
        info = next((w for w in self.list_wallets() if w.name == coldkey_name), None)
        return info.hotkeys if info else []

    def try_read_coldkey_ss58(self, name: str) -> str | None:
        try:
            wallet = Wallet(name=name, hotkey="default", path=str(self.wallet_path))
            return wallet.coldkeypub.ss58_address
        except (KeyFileError, Exception):
            return None

    def remove_wallet(self, coldkey_name: str) -> None:
        self.validate_wallet_access(coldkey_name)
        wallet_dir = self.wallet_path / coldkey_name
        shutil.rmtree(wallet_dir)
        logger.info("Deleted wallet directory user=%s name=%s", self.user_id, coldkey_name)
