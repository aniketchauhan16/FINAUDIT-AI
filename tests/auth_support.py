"""Helpers for API tests that exercise real SQLite-backed sessions."""

from datetime import datetime, timezone
from uuid import uuid4

from app import auth, database
from app.models import UserRole


TEST_PASSWORD = "Test-Password-42!"


def create_test_session(
    client, role: UserRole = UserRole.reviewer, username: str | None = None
) -> tuple[str, str]:
    username = username or f"test_{uuid4().hex[:12]}"
    database.create_user(
        str(uuid4()),
        username,
        auth.hash_password(TEST_PASSWORD),
        role,
        datetime.now(timezone.utc).isoformat(),
    )
    response = client.post(
        "/auth/login", json={"username": username, "password": TEST_PASSWORD}
    )
    if response.status_code != 200:
        raise AssertionError(f"Test user login failed: {response.status_code} {response.text}")
    token = response.json()["access_token"]
    return username, token


def login_test_user(
    client, role: UserRole = UserRole.reviewer, username: str | None = None
) -> str:
    username, token = create_test_session(client, role, username)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return username
