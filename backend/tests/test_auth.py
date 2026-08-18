from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import SESSION_COOKIE_NAME, app
from app.services import auth_store


@pytest.fixture()
def isolated_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setattr("app.services.auth_store.DATA_ROOT", tmp_path)
    monkeypatch.setenv("VERIFICATION_BOOTSTRAP_USERNAME", "admin")
    monkeypatch.setenv("VERIFICATION_BOOTSTRAP_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("VERIFICATION_SESSION_SECURE", "false")
    return TestClient(app)


def login(client: TestClient, *, replace_existing: bool = False) -> object:
    return client.post(
        "/api/v1/auth/login",
        json={
            "username": "admin",
            "password": "correct-horse-battery-staple",
            "replaceExisting": replace_existing,
        },
    )


def test_bootstrap_password_can_be_read_from_a_mounted_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.auth_store.DATA_ROOT", tmp_path / "data")
    secret = tmp_path / "bootstrap-password"
    secret.write_text("mounted-reviewer-password\n", encoding="utf-8")
    monkeypatch.setenv("VERIFICATION_BOOTSTRAP_USERNAME", "mounted-admin")
    monkeypatch.delenv("VERIFICATION_BOOTSTRAP_PASSWORD", raising=False)
    monkeypatch.setenv("VERIFICATION_BOOTSTRAP_PASSWORD_FILE", str(secret))

    created = auth_store.ensure_bootstrap_user()

    assert created is not None
    assert created.username == "mounted-admin"
    assert auth_store.authenticate("mounted-admin", "mounted-reviewer-password", force=False)


def test_api_requires_a_session_except_health_version_and_login(isolated_auth: TestClient) -> None:
    assert isolated_auth.get("/api/v1/health").status_code == 200
    assert isolated_auth.get("/api/v1/version").status_code == 200
    assert isolated_auth.get("/api/v1/cases").status_code == 401
    assert isolated_auth.get("/api/v1/capabilities").status_code == 401

    response = login(isolated_auth)
    assert response.status_code == 200, response.text
    assert SESSION_COOKIE_NAME in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    assert response.json()["username"] == "admin"
    assert isolated_auth.get("/api/v1/auth/me").json()["username"] == "admin"
    assert isolated_auth.get("/api/v1/capabilities").status_code == 200


def test_second_login_requires_confirmation_and_revokes_first_session(
    isolated_auth: TestClient,
) -> None:
    first = login(isolated_auth)
    assert first.status_code == 200
    token = isolated_auth.cookies.get(SESSION_COOKIE_NAME)
    assert token

    second_client = TestClient(app)
    conflict = login(second_client)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "active_session_exists"

    confirmed = login(second_client, replace_existing=True)
    assert confirmed.status_code == 200
    assert isolated_auth.get("/api/v1/auth/me").status_code == 401
    assert second_client.get("/api/v1/auth/me").status_code == 200


def test_logout_and_server_side_preferences_are_user_scoped(isolated_auth: TestClient) -> None:
    assert login(isolated_auth).status_code == 200
    updated = isolated_auth.put(
        "/api/v1/preferences",
        json={"preferences": {"queueFilters": {"outcome": "needs_review"}, "readerMode": "ocr"}},
    )
    assert updated.status_code == 200
    assert updated.json()["preferences"]["readerMode"] == "ocr"
    assert isolated_auth.get("/api/v1/preferences").json() == updated.json()

    queue_preferences = isolated_auth.put(
        "/api/v1/preferences/queue",
        json={"outcome": "needs_review", "showRemoved": True},
    )
    assert queue_preferences.status_code == 200
    assert isolated_auth.get("/api/v1/preferences/queue").json() == {
        "outcome": "needs_review",
        "showRemoved": True,
    }

    analysis_preferences = isolated_auth.put(
        "/api/v1/preferences/analysis",
        json={"readerMode": "llm"},
    )
    assert analysis_preferences.status_code == 200
    assert isolated_auth.get("/api/v1/preferences/analysis").json() == {
        "readerMode": "llm",
    }
    assert isolated_auth.put(
        "/api/v1/preferences/analysis", json={"readerMode": "both"}
    ).status_code == 422
    assert isolated_auth.put(
        "/api/v1/preferences/analysis", json={"readerMode": "invalid"}
    ).status_code == 422

    logout = isolated_auth.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert isolated_auth.get("/api/v1/preferences").status_code == 401


def test_admin_can_add_three_users_and_password_reset_revokes_session(
    isolated_auth: TestClient,
) -> None:
    first = auth_store.create_user("first", "first-reviewer-password")
    auth_store.create_user("second", "second-reviewer-password")
    auth_store.create_user("third", "third-reviewer-password")
    assert [user.username for user in auth_store.list_users()] == ["first", "second", "third"]
    with pytest.raises(ValueError, match="At most 3"):
        auth_store.create_user("fourth", "fourth-reviewer-password")

    token, _ = auth_store.authenticate("first", "first-reviewer-password", force=False)  # type: ignore[misc]
    assert auth_store.get_session(token) is not None
    reset = auth_store.reset_password("first", "replacement-password")
    assert reset.user_id == first.user_id
    assert auth_store.get_session(token) is None
    assert auth_store.authenticate("first", "first-reviewer-password", force=False) is None
    assert auth_store.authenticate("first", "replacement-password", force=False) is not None
