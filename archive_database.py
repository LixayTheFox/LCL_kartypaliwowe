from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from fuel_analysis import (
    ArchivedReportInfo,
    FuelRecord,
    _date_from_iso,
    _datetime_from_iso,
    build_archive_payload,
    records_from_archive_payload,
)


TABLE_NAME = "fuel_insight_archives"
DEFAULT_PORT = 5432
DEFAULT_SSLMODE = "prefer"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob_from_bytes(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _protect_secret(secret: str) -> str:
    if os.name != "nt" or not secret:
        return ""
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _blob_from_bytes(secret.encode("utf-8"))
        output_blob = _DataBlob()
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "FuelInsight",
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
        _ = input_buffer
        if not ok:
            return ""
        try:
            protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return base64.b64encode(protected).decode("ascii")
        finally:
            kernel32.LocalFree(output_blob.pbData)
    except Exception:
        return ""


def _unprotect_secret(secret: object) -> str:
    if os.name != "nt" or not secret:
        return ""
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        encrypted = base64.b64decode(str(secret))
        input_blob, input_buffer = _blob_from_bytes(encrypted)
        output_blob = _DataBlob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
        _ = input_buffer
        if not ok:
            return ""
        try:
            raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
            return raw.decode("utf-8")
        finally:
            kernel32.LocalFree(output_blob.pbData)
    except Exception:
        return ""


@dataclass(slots=True)
class DatabaseConfig:
    host: str = ""
    port: int = DEFAULT_PORT
    database: str = ""
    username: str = ""
    password: str = ""
    sslmode: str = DEFAULT_SSLMODE
    connect_timeout: int = 5

    @property
    def is_complete(self) -> bool:
        return bool(self.host.strip() and self.database.strip() and self.username.strip())


def load_database_config(path: str | Path) -> DatabaseConfig:
    target = Path(path)
    if not target.exists():
        return DatabaseConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DatabaseConfig()
    if not isinstance(raw, dict):
        return DatabaseConfig()
    try:
        port = int(raw.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    try:
        connect_timeout = int(raw.get("connect_timeout") or 5)
    except (TypeError, ValueError):
        connect_timeout = 5
    return DatabaseConfig(
        host=str(raw.get("host") or "").strip(),
        port=port,
        database=str(raw.get("database") or "").strip(),
        username=str(raw.get("username") or "").strip(),
        password=_unprotect_secret(raw.get("password_protected")) or str(raw.get("password") or ""),
        sslmode=str(raw.get("sslmode") or DEFAULT_SSLMODE).strip() or DEFAULT_SSLMODE,
        connect_timeout=max(1, connect_timeout),
    )


def save_database_config(path: str | Path, config: DatabaseConfig) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    protected_password = _protect_secret(config.password)
    payload = {
        "host": config.host.strip(),
        "port": int(config.port),
        "database": config.database.strip(),
        "username": config.username.strip(),
        "sslmode": config.sslmode.strip() or DEFAULT_SSLMODE,
        "connect_timeout": int(config.connect_timeout),
    }
    if protected_password:
        payload["password_protected"] = protected_password
    else:
        payload["password"] = config.password
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _psycopg():
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Brakuje biblioteki psycopg. Zainstaluj zależności i zbuduj aplikację ponownie."
        ) from exc
    return psycopg


def _connect(config: DatabaseConfig):
    if not config.is_complete:
        raise ValueError("Uzupełnij adres, nazwę bazy i login PostgreSQL.")
    psycopg = _psycopg()
    return psycopg.connect(
        host=config.host.strip(),
        port=int(config.port),
        dbname=config.database.strip(),
        user=config.username.strip(),
        password=config.password,
        sslmode=config.sslmode.strip() or DEFAULT_SSLMODE,
        connect_timeout=int(config.connect_timeout),
    )


def _ensure_schema(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id BIGSERIAL PRIMARY KEY,
                source_file TEXT NOT NULL,
                imported_at TIMESTAMPTZ NOT NULL,
                date_from DATE,
                date_to DATE,
                record_count INTEGER NOT NULL,
                payload JSONB NOT NULL
            )
            """
        )
        cursor.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_imported_at
            ON {TABLE_NAME} (imported_at DESC)
            """
        )


def test_database_connection(config: DatabaseConfig) -> None:
    with _connect(config) as connection:
        _ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()


def save_database_archive(
    config: DatabaseConfig,
    records: Iterable[FuelRecord],
    source_file: str,
    imported_at: datetime | None = None,
) -> int:
    payload = build_archive_payload(records, source_file, imported_at)
    with _connect(config) as connection:
        _ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {TABLE_NAME}
                    (source_file, imported_at, date_from, date_to, record_count, payload)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    str(payload.get("source_file") or source_file),
                    payload.get("imported_at"),
                    payload.get("date_from"),
                    payload.get("date_to"),
                    int(payload.get("record_count") or 0),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            row = cursor.fetchone()
            return int(row[0])


def _payload_from_database(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = json.loads(value)
        if isinstance(raw, dict):
            return raw
    raise ValueError("Archiwum z bazy danych ma nieprawidłowy format.")


def list_database_archives(config: DatabaseConfig) -> list[ArchivedReportInfo]:
    with _connect(config) as connection:
        _ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, source_file, imported_at, date_from, date_to, record_count
                FROM {TABLE_NAME}
                ORDER BY imported_at DESC, id DESC
                """
            )
            entries = []
            for remote_id, source_file, imported_at, date_from, date_to, record_count in cursor.fetchall():
                entries.append(
                    ArchivedReportInfo(
                        source_file=str(source_file or "raport"),
                        imported_at=imported_at if isinstance(imported_at, datetime) else _datetime_from_iso(imported_at),
                        date_from=date_from if hasattr(date_from, "isoformat") else _date_from_iso(date_from),
                        date_to=date_to if hasattr(date_to, "isoformat") else _date_from_iso(date_to),
                        record_count=int(record_count or 0),
                        remote_id=int(remote_id),
                        storage="database",
                    )
                )
            return entries


def load_database_archive(config: DatabaseConfig, remote_id: int) -> tuple[dict[str, object], list[FuelRecord]]:
    with _connect(config) as connection:
        _ensure_schema(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT payload FROM {TABLE_NAME} WHERE id = %s",
                (int(remote_id),),
            )
            row = cursor.fetchone()
    if not row:
        raise ValueError("Nie znaleziono raportu w bazie danych.")
    payload = _payload_from_database(row[0])
    return payload, records_from_archive_payload(payload)
