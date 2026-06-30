from __future__ import annotations

from enum import Enum


class FailureKind(str, Enum):
    RETRYABLE = "retryable"
    UNRECOVERABLE = "unrecoverable"


_UNRECOVERABLE_TOKENS = (
    "subnet",
    "does not exist",
    "not found",
    "wallet",
    "keyfile",
    "cannot read",
    "deleted",
    "no such",
)

_RETRYABLE_TOKENS = (
    "insufficient",
    "balance",
    "price",
    "pricelimit",
    "price limit",
    "chain connection",
    "timeout",
    "network",
    "websocket",
    "password",
    "invalid password",
    "unlock",
    "temporarily",
    "nonce",
    "busy",
    "already registered",
)


def classify_registration_failure(message: str) -> FailureKind:
    lowered = message.lower()
    if any(token in lowered for token in _UNRECOVERABLE_TOKENS):
        if "insufficient" in lowered or "balance" in lowered:
            return FailureKind.RETRYABLE
        if "password" in lowered or "unlock" in lowered:
            return FailureKind.RETRYABLE
        if "already registered" in lowered:
            return FailureKind.RETRYABLE
        return FailureKind.UNRECOVERABLE
    if any(token in lowered for token in _RETRYABLE_TOKENS):
        return FailureKind.RETRYABLE
    return FailureKind.RETRYABLE
