from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


APP_ENV_PREFIX = "FUEL_INSIGHT"


def _path_from_env(name: str, default: Path | str | None = None) -> Path | None:
    raw = os.getenv(name)
    if raw:
        return Path(raw).expanduser()
    if default is None:
        return None
    return Path(default).expanduser()


def default_data_dir() -> Path:
    configured = _path_from_env(f"{APP_ENV_PREFIX}_DATA_DIR")
    if configured:
        return configured
    if os.name == "nt":
        return Path(os.getenv("APPDATA", Path.home())) / "FuelInsight"
    xdg_data_home = os.getenv("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "fuel-insight"
    return Path.home() / ".local" / "share" / "fuel-insight"


DATA_DIR = default_data_dir()
MAPPING_PATH = _path_from_env(f"{APP_ENV_PREFIX}_DRIVER_MAPPING", DATA_DIR / "kierowcy.json")
DB_CONFIG_PATH = _path_from_env(f"{APP_ENV_PREFIX}_DB_CONFIG", DATA_DIR / "baza_postgresql.json")
ARCHIVE_DIR = _path_from_env(f"{APP_ENV_PREFIX}_ARCHIVE_DIR", DATA_DIR / "archiwum")
SERVICE_INPUT_DIR = _path_from_env(f"{APP_ENV_PREFIX}_INPUT_DIR", DATA_DIR / "inbox")
SERVICE_OUTPUT_DIR = _path_from_env(f"{APP_ENV_PREFIX}_OUTPUT_DIR", DATA_DIR / "outbox")
SERVICE_PROCESSED_DIR = _path_from_env(f"{APP_ENV_PREFIX}_PROCESSED_DIR", DATA_DIR / "processed")
SERVICE_FAILED_DIR = _path_from_env(f"{APP_ENV_PREFIX}_FAILED_DIR", DATA_DIR / "failed")
LOG_FILE = _path_from_env(f"{APP_ENV_PREFIX}_LOG_FILE")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


WEB_HOST = os.getenv(f"{APP_ENV_PREFIX}_WEB_HOST", "0.0.0.0")
WEB_PORT = env_int(f"{APP_ENV_PREFIX}_WEB_PORT", 8000)
WEB_MAX_UPLOAD_MB = env_int(f"{APP_ENV_PREFIX}_WEB_MAX_UPLOAD_MB", 100)


def configure_logging(name: str = "fuel_insight") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level_name = os.getenv(f"{APP_ENV_PREFIX}_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if LOG_FILE:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
