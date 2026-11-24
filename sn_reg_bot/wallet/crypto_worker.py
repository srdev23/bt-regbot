from __future__ import annotations

"""Helpers that run wallet password KDF off the event loop.

The bittensor_wallet decryption is a compiled (Rust) extension whose Argon2 KDF
holds the GIL while running. Running it in the main process — even via a thread —
freezes the asyncio event loop and blocks every other user. These functions are
designed to run inside a separate worker process so unlocks never block the bot
and multiple users can unlock concurrently.
"""

PROBE_OK = "ok"
PROBE_INVALID = "invalid"


def probe_password(wallet_name: str, hotkey: str, wallet_path: str, password: str) -> str:
    """Validate a coldkey password. Returns 'ok', 'invalid', or 'error:<msg>'.

    Runs in a worker process; arguments and return value are simple strings so
    they pickle cleanly across the process boundary.
    """
    try:
        from bittensor_wallet import Wallet
        from bittensor_wallet.errors import KeyFileError, PasswordError
    except Exception as exc:  # pragma: no cover - import failure in worker
        return f"error:import failed: {exc}"

    try:
        wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
        wallet.get_coldkey(password=password)
        return PROBE_OK
    except PasswordError:
        return PROBE_INVALID
    except KeyFileError as exc:
        return f"error:{exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return f"error:{exc}"
