from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from .models import UserRecord


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str
    is_referee: bool
    is_observer: bool = False


class PasswordHasher:
    """Small stdlib-only password hasher for the private MVP deployment.

    scrypt is intentionally used instead of storing plaintext credentials or
    adding another dependency before the identity model settles.
    """

    algorithm = "scrypt"

    @classmethod
    def hash(cls, password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return "$".join((cls.algorithm, _b64(salt), _b64(digest)))

    @classmethod
    def verify(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, salt_text, digest_text = encoded.split("$", 2)
            if algorithm != cls.algorithm:
                return False
            salt = _unb64(salt_text)
            expected = _unb64(digest_text)
            actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False


class SessionStore:
    def __init__(self) -> None:
        self._tokens: dict[str, AuthenticatedUser] = {}

    def issue(self, user: UserRecord) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = AuthenticatedUser(user.user_id, user.username, user.is_referee, user.is_observer)
        return token

    def get(self, token: str | None) -> AuthenticatedUser | None:
        if not token:
            return None
        return self._tokens.get(token)

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
