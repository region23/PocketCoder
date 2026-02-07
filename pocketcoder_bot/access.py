from __future__ import annotations

from dataclasses import dataclass
import os


def _parse_ids(raw: str | None) -> set[int]:
    if raw is None:
        return set()
    result: set[int] = set()
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError:
            continue
    return result


@dataclass(frozen=True)
class AccessPolicy:
    allowed_user_ids: set[int]
    allowed_chat_ids: set[int]

    @classmethod
    def from_env(cls) -> AccessPolicy:
        return cls(
            allowed_user_ids=_parse_ids(os.getenv("POCKETCODER_ALLOWED_USER_IDS")),
            allowed_chat_ids=_parse_ids(os.getenv("POCKETCODER_ALLOWED_CHAT_IDS")),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_user_ids or self.allowed_chat_ids)


def is_allowed(
    policy: AccessPolicy,
    *,
    user_id: int | None,
    chat_id: int | None,
) -> bool:
    if not policy.enabled:
        return True
    if policy.allowed_user_ids and user_id not in policy.allowed_user_ids:
        return False
    if policy.allowed_chat_ids and chat_id not in policy.allowed_chat_ids:
        return False
    return True

