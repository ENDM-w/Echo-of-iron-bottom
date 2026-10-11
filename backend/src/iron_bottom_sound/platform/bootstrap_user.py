from __future__ import annotations

import getpass
import os
import uuid
from pathlib import Path

from .auth import PasswordHasher
from .models import UserRecord
from .repository import PlatformRepository


def create_initial_referee(database_path: str | Path, username: str, password: str) -> UserRecord:
    repository = PlatformRepository(database_path)
    if repository.has_users():
        raise ValueError("A platform account already exists; create later accounts from the referee interface")
    username = username.strip()
    if not username or len(username) > 80:
        raise ValueError("Username must contain 1 to 80 characters")
    if len(password) < 8 or len(password) > 256:
        raise ValueError("Password must contain 8 to 256 characters")
    user = UserRecord(
        user_id=str(uuid.uuid4()),
        username=username,
        password_hash=PasswordHasher.hash(password),
        is_referee=True,
    )
    repository.create_user(user)
    return user


def main() -> None:
    project_root = Path(__file__).resolve().parents[4]
    database_path = Path(os.environ.get(
        "IBS_DB_PATH",
        str(project_root / "backend" / "iron-bottom-sound.sqlite3"),
    ))
    username = input("Initial referee username: ").strip()
    password = getpass.getpass("Initial referee password (min 8 chars): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    try:
        user = create_initial_referee(database_path, username, password)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"Created initial referee account '{user.username}'.")


if __name__ == "__main__":
    main()
