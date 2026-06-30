from __future__ import annotations

from enum import Enum


class UserAccessStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SUSPENDED = "suspended"

    def label(self) -> str:
        return {
            UserAccessStatus.PENDING: "⏳ Pending",
            UserAccessStatus.APPROVED: "✅ Approved",
            UserAccessStatus.SUSPENDED: "🚫 Suspended",
        }[self]
