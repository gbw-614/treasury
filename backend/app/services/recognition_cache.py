from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Literal

from pydantic import BaseModel

from app.services import case_store

ReaderType = Literal["ocr", "llm"]
CACHE_LOCK = RLock()

_TABLE_LOCK = Lock()
_INITIALIZED_DATABASES: set[Path] = set()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_table(connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_recognition_cache (
            cache_key TEXT PRIMARY KEY,
            reader_type TEXT NOT NULL CHECK (reader_type IN ('ocr', 'llm')),
            image_sha256 TEXT NOT NULL,
            reader_version TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _connect() -> sqlite3.Connection:
    connection = case_store._connect()
    resolved = case_store._paths()[0].resolve()
    with _TABLE_LOCK:
        if resolved not in _INITIALIZED_DATABASES:
            _ensure_table(connection)
            connection.commit()
            _INITIALIZED_DATABASES.add(resolved)
    return connection


def get[ModelT: BaseModel](cache_key: str, model: type[ModelT]) -> ModelT | None:
    with CACHE_LOCK, _connect() as connection:
        row = connection.execute(
            "SELECT result_json FROM verification_recognition_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """UPDATE verification_recognition_cache
               SET last_used_at = ?, hit_count = hit_count + 1 WHERE cache_key = ?""",
            (_now(), cache_key),
        )
    try:
        return model.model_validate_json(row["result_json"])
    except (ValueError, TypeError):
        # A stale or corrupt entry is never allowed to break recognition.
        with CACHE_LOCK, _connect() as connection:
            connection.execute(
                "DELETE FROM verification_recognition_cache WHERE cache_key = ?", (cache_key,)
            )
        return None


def put(
    cache_key: str,
    *,
    reader_type: ReaderType,
    image_sha256: str,
    reader_version: str,
    result: BaseModel,
) -> None:
    now = _now()
    with CACHE_LOCK, _connect() as connection:
        connection.execute(
            """
            INSERT INTO verification_recognition_cache (
                cache_key, reader_type, image_sha256, reader_version, result_json,
                created_at, last_used_at, hit_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(cache_key) DO UPDATE SET
                result_json = excluded.result_json,
                last_used_at = excluded.last_used_at
            """,
            (
                cache_key,
                reader_type,
                image_sha256,
                reader_version,
                result.model_dump_json(by_alias=True),
                now,
                now,
            ),
        )


def stats() -> dict[str, int]:
    with CACHE_LOCK, _connect() as connection:
        rows = connection.execute(
            "SELECT reader_type, COUNT(*) AS count FROM verification_recognition_cache GROUP BY reader_type"
        ).fetchall()
    counts = {row["reader_type"]: int(row["count"]) for row in rows}
    ocr = counts.get("ocr", 0)
    llm = counts.get("llm", 0)
    return {"ocr": ocr, "llm": llm, "total": ocr + llm}


def clear() -> dict[str, int]:
    before = stats()
    with CACHE_LOCK, _connect() as connection:
        connection.execute("DELETE FROM verification_recognition_cache")
    return before
