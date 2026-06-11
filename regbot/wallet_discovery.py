from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WalletInfo:
    name: str
    hotkeys: list[str]


def discover_wallets(wallet_path: Path) -> list[WalletInfo]:
    if not wallet_path.is_dir():
        return []

    wallets: list[WalletInfo] = []
    for entry in sorted(wallet_path.iterdir()):
        if not entry.is_dir():
            continue
        coldkey = entry / "coldkey"
        coldkeypub = entry / "coldkeypub.txt"
        if not coldkey.exists() and not coldkeypub.exists():
            continue

        hotkeys_dir = entry / "hotkeys"
        hotkeys: list[str] = []
        if hotkeys_dir.is_dir():
            for hk in sorted(hotkeys_dir.iterdir()):
                if hk.is_file() and not hk.name.endswith("pub.txt"):
                    hotkeys.append(hk.name)

        if hotkeys:
            wallets.append(WalletInfo(name=entry.name, hotkeys=hotkeys))

    return wallets
