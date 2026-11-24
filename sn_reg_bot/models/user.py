from __future__ import annotations

from dataclasses import dataclass

from sn_reg_bot.models.access import UserAccessStatus


@dataclass
class UserRecord:
    telegram_user_id: int
    created_at: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    last_seen_at: str | None = None
    access_status: UserAccessStatus = UserAccessStatus.PENDING

    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return " ".join(parts)
        return f"id:{self.telegram_user_id}"

    def full_label(self) -> str:
        name = self.display_name()
        if self.username and not name.startswith("@"):
            return name
        extra = " ".join(p for p in (self.first_name, self.last_name) if p)
        if extra and not name.startswith("@"):
            return f"{name} ({extra})"
        if extra and name.startswith("@"):
            return f"{name} · {extra}"
        return name

    @classmethod
    def from_row(cls, row: dict) -> UserRecord:
        raw_status = row.get("access_status") or UserAccessStatus.APPROVED.value
        return cls(
            telegram_user_id=int(row["telegram_user_id"]),
            created_at=str(row["created_at"]),
            username=row.get("username"),
            first_name=row.get("first_name"),
            last_name=row.get("last_name"),
            last_seen_at=row.get("last_seen_at"),
            access_status=UserAccessStatus(raw_status),
        )

    def is_approved(self) -> bool:
        return self.access_status == UserAccessStatus.APPROVED
