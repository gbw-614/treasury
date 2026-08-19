"""Small SQLite-backed identity store for the self-hosted reviewer console.

It intentionally shares the existing data directory/database but owns separate
tables, so adding authentication never rewrites or migrates case records.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.schemas.auth import CurrentUser

DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
DATA_ROOT = Path(os.environ.get("VERIFICATION_DATA_DIR") or DEFAULT_DATA_ROOT)
PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 1
MAX_ENABLED_USERS = 3

_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_DATABASES: set[Path] = set()


@dataclass(frozen=True)
class AuthenticatedSession:
    user: CurrentUser
    expires_at: datetime


def _database_path() -> Path:
    return DATA_ROOT / "verification.sqlite3"


def _connect() -> sqlite3.Connection:
    database = _database_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    resolved = database.resolve()
    with _SCHEMA_LOCK:
        if resolved not in _INITIALIZED_DATABASES:
            _initialize_schema(connection)
            _INITIALIZED_DATABASES.add(resolved)
    return connection


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS verification_users (
            user_id TEXT PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            disabled_at TEXT
        );
        CREATE TABLE IF NOT EXISTS verification_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES verification_users(user_id),
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            revoked_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS verification_sessions_active_user
            ON verification_sessions(user_id, expires_at)
            WHERE revoked_at IS NULL;
        CREATE TABLE IF NOT EXISTS verification_user_preferences (
            user_id TEXT PRIMARY KEY REFERENCES verification_users(user_id),
            preferences_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS verification_auth_audit (
            audit_id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES verification_users(user_id),
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            details_json TEXT NOT NULL
        );
        """
    )
    connection.commit()


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _password_hash(password: str, *, salt: bytes | None = None) -> str:
    """Use stdlib scrypt, avoiding a platform-specific native dependency.

    Argon2 would be equally suitable. scrypt gives the local Docker build a
    memory-hard password hash without needing an additional compiler/wheel.
    """
    return _password_hash_with_params(
        password,
        salt or secrets.token_bytes(16),
        PASSWORD_SCRYPT_N,
        PASSWORD_SCRYPT_R,
        PASSWORD_SCRYPT_P,
    )


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt, _ = stored.split("$", 6)
        if algorithm != "scrypt":
            return False
        raw_salt = base64.urlsafe_b64decode(salt.encode("ascii"))
        candidate = _password_hash_with_params(password, raw_salt, int(n), int(r), int(p))
        return hmac.compare_digest(candidate, stored)
    except (ValueError, TypeError, UnicodeError):
        return False


def _password_hash_with_params(password: str, salt: bytes, n: int, r: int, p: int) -> str:
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        n=n,
        r=r,
        p=p,
        salt=base64.urlsafe_b64encode(salt).decode("ascii"),
        digest=base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def _user_from_row(row: sqlite3.Row) -> CurrentUser:
    return CurrentUser(user_id=row["user_id"], username=row["username"], created_at=_parse_timestamp(row["created_at"]))


def _audit(connection: sqlite3.Connection, user_id: str | None, event_type: str, **details: object) -> None:
    connection.execute(
        "INSERT INTO verification_auth_audit (audit_id, user_id, event_type, created_at, details_json) VALUES (?, ?, ?, ?, ?)",
        (str(uuid4()), user_id, event_type, _timestamp(), json.dumps(details, sort_keys=True)),
    )


def create_user(username: str, password: str) -> CurrentUser:
    """Administrative/bootstrap-only user creation. There is deliberately no API route."""
    normalized = username.strip()
    if not normalized:
        raise ValueError("Username is required")
    if len(password) < 8:
        raise ValueError("Password must contain at least eight characters")
    with _connect() as connection:
        enabled_count = connection.execute(
            "SELECT COUNT(*) FROM verification_users WHERE disabled_at IS NULL"
        ).fetchone()[0]
        if enabled_count >= MAX_ENABLED_USERS:
            raise ValueError(f"At most {MAX_ENABLED_USERS} enabled users are supported")
        user = CurrentUser(user_id=str(uuid4()), username=normalized, created_at=_now())
        try:
            connection.execute(
                "INSERT INTO verification_users (user_id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user.user_id, user.username, _password_hash(password), user.created_at.isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Username already exists") from exc
        _audit(connection, user.user_id, "user_created", bootstrap=True)
    return user


def list_users() -> tuple[CurrentUser, ...]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM verification_users WHERE disabled_at IS NULL ORDER BY username COLLATE NOCASE"
        ).fetchall()
    return tuple(_user_from_row(row) for row in rows)


def reset_password(username: str, password: str) -> CurrentUser:
    if len(password) < 8:
        raise ValueError("Password must contain at least eight characters")
    now = _timestamp()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM verification_users WHERE username = ? COLLATE NOCASE AND disabled_at IS NULL",
            (username.strip(),),
        ).fetchone()
        if row is None:
            raise ValueError("User not found")
        connection.execute(
            "UPDATE verification_users SET password_hash = ? WHERE user_id = ?",
            (_password_hash(password), row["user_id"]),
        )
        connection.execute(
            "UPDATE verification_sessions SET revoked_at = ?, revoked_reason = 'password_reset' WHERE user_id = ? AND revoked_at IS NULL",
            (now, row["user_id"]),
        )
        _audit(connection, row["user_id"], "password_reset")
    return _user_from_row(row)


def has_users() -> bool:
    with _connect() as connection:
        return bool(
            connection.execute(
                "SELECT 1 FROM verification_users WHERE disabled_at IS NULL LIMIT 1"
            ).fetchone()
        )


def _bootstrap_password() -> str:
    direct = os.getenv("VERIFICATION_BOOTSTRAP_PASSWORD") or ""
    if direct:
        return direct
    path = (os.getenv("VERIFICATION_BOOTSTRAP_PASSWORD_FILE") or "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError:
        return ""


def ensure_bootstrap_user() -> CurrentUser | None:
    """Create exactly one initial admin from environment or a mounted secret."""
    username = (os.getenv("VERIFICATION_BOOTSTRAP_USERNAME") or "").strip()
    password = _bootstrap_password()
    if not username or not password:
        return None
    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM verification_users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
    if existing:
        return _user_from_row(existing)
    try:
        return create_user(username, password)
    except ValueError:
        # A concurrently created bootstrap user is fine; never change its password.
        with _connect() as connection:
            row = connection.execute(
                "SELECT * FROM verification_users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
        if row is not None:
            return _user_from_row(row)
        raise


def session_ttl() -> timedelta:
    try:
        hours = max(1, min(24 * 30, int(os.getenv("VERIFICATION_SESSION_HOURS", "12"))))
    except ValueError:
        hours = 12
    return timedelta(hours=hours)


def authenticate(username: str, password: str, *, force: bool) -> tuple[str, AuthenticatedSession] | None:
    """Return ``None`` for invalid credentials; raise ActiveSessionError for confirmation."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM verification_users WHERE username = ? COLLATE NOCASE AND disabled_at IS NULL",
            (username.strip(),),
        ).fetchone()
        if row is None or not _verify_password(password, row["password_hash"]):
            return None
        now = _now()
        active = connection.execute(
            "SELECT session_id FROM verification_sessions WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?",
            (row["user_id"], _timestamp(now)),
        ).fetchone()
        if active and not force:
            raise ActiveSessionError
        if active:
            connection.execute(
                "UPDATE verification_sessions SET revoked_at = ?, revoked_reason = 'replaced_by_new_login' WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?",
                (_timestamp(now), row["user_id"], _timestamp(now)),
            )
            _audit(connection, row["user_id"], "session_replaced")
        token = secrets.token_urlsafe(32)
        expires = now + session_ttl()
        connection.execute(
            "INSERT INTO verification_sessions (session_id, user_id, token_hash, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid4()), row["user_id"], _hash_token(token), _timestamp(now), _timestamp(now), _timestamp(expires)),
        )
        _audit(connection, row["user_id"], "login")
        return token, AuthenticatedSession(user=_user_from_row(row), expires_at=expires)


class ActiveSessionError(Exception):
    pass


def get_session(token: str | None) -> AuthenticatedSession | None:
    if not token:
        return None
    now = _now()
    token_hash = _hash_token(token)
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT u.user_id, u.username, u.created_at, s.expires_at
            FROM verification_sessions s
            JOIN verification_users u ON u.user_id = s.user_id
            WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND u.disabled_at IS NULL
            """,
            (token_hash, _timestamp(now)),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE verification_sessions SET last_seen_at = ? WHERE token_hash = ?", (_timestamp(now), token_hash)
        )
    return AuthenticatedSession(user=_user_from_row(row), expires_at=_parse_timestamp(row["expires_at"]))


def revoke_session(token: str | None, *, reason: str = "logout") -> None:
    if not token:
        return
    now = _timestamp()
    with _connect() as connection:
        row = connection.execute(
            "SELECT user_id FROM verification_sessions WHERE token_hash = ? AND revoked_at IS NULL", (_hash_token(token),)
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE verification_sessions SET revoked_at = ?, revoked_reason = ? WHERE token_hash = ?",
                (now, reason, _hash_token(token)),
            )
            _audit(connection, row["user_id"], reason)


def get_preferences(user_id: str) -> dict[str, object]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT preferences_json FROM verification_user_preferences WHERE user_id = ?", (user_id,)
        ).fetchone()
    return json.loads(row["preferences_json"]) if row else {}


def save_preferences(user_id: str, preferences: dict[str, object]) -> dict[str, object]:
    payload = json.dumps(preferences, separators=(",", ":"), sort_keys=True)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO verification_user_preferences (user_id, preferences_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET preferences_json = excluded.preferences_json, updated_at = excluded.updated_at
            """,
            (user_id, payload, _timestamp()),
        )
    return preferences


def get_preference_scope(user_id: str, scope: str) -> dict[str, object]:
    value = get_preferences(user_id).get(scope, {})
    return value if isinstance(value, dict) else {}


def save_preference_scope(
    user_id: str, scope: str, preferences: dict[str, object]
) -> dict[str, object]:
    root = get_preferences(user_id)
    root[scope] = preferences
    save_preferences(user_id, root)
    return preferences
