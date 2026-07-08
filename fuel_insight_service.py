from __future__ import annotations

import argparse
import shutil
import signal
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from archive_database import (
    DatabaseConfig,
    save_database_archive,
    test_database_connection,
    load_database_config,
)
from fuel_analysis import (
    analyze_records,
    export_csv,
    export_xlsx,
    load_driver_mapping,
    load_single_report,
    save_archive_report,
)
from runtime_config import (
    ARCHIVE_DIR,
    DB_CONFIG_PATH,
    MAPPING_PATH,
    SERVICE_FAILED_DIR,
    SERVICE_INPUT_DIR,
    SERVICE_OUTPUT_DIR,
    SERVICE_PROCESSED_DIR,
    configure_logging,
    env_bool,
    env_float,
    env_int,
)


LOGGER = configure_logging("fuel_insight.service")
STOP_REQUESTED = False


@dataclass(slots=True)
class ServiceConfig:
    input_dir: Path
    output_dir: Path
    processed_dir: Path
    failed_dir: Path
    archive_dir: Path
    mapping_path: Path
    db_config_path: Path
    db_config: DatabaseConfig
    minimum_distance: float
    poll_interval: int
    require_db: bool
    export_csv: bool


def _signal_stop(_signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    LOGGER.info("Stop requested")


def _unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _timestamped_name(source: Path, suffix: str) -> str:
    safe_stem = "".join(char if char.isalnum() or char in "-_" else "_" for char in source.stem)
    safe_stem = safe_stem.strip("_") or "raport"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{safe_stem}{suffix}"


def _discover_reports(input_dir: Path) -> list[Path]:
    if not input_dir.exists():
        return []
    return sorted(
        path
        for path in input_dir.glob("*.xlsx")
        if path.is_file() and not path.name.startswith("~$")
    )


def _ensure_directories(config: ServiceConfig) -> None:
    for directory in (
        config.input_dir,
        config.output_dir,
        config.processed_dir,
        config.failed_dir,
        config.archive_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def _build_config(args: argparse.Namespace) -> ServiceConfig:
    db_config_path = Path(args.db_config or DB_CONFIG_PATH).expanduser()
    db_config = load_database_config(db_config_path)
    return ServiceConfig(
        input_dir=Path(args.input_dir or SERVICE_INPUT_DIR).expanduser(),
        output_dir=Path(args.output_dir or SERVICE_OUTPUT_DIR).expanduser(),
        processed_dir=Path(args.processed_dir or SERVICE_PROCESSED_DIR).expanduser(),
        failed_dir=Path(args.failed_dir or SERVICE_FAILED_DIR).expanduser(),
        archive_dir=Path(args.archive_dir or ARCHIVE_DIR).expanduser(),
        mapping_path=Path(args.mapping_path or MAPPING_PATH).expanduser(),
        db_config_path=db_config_path,
        db_config=db_config,
        minimum_distance=float(args.minimum_distance),
        poll_interval=max(5, int(args.poll_interval)),
        require_db=bool(args.require_db),
        export_csv=bool(args.export_csv),
    )


def _validate_database(config: ServiceConfig) -> None:
    if not config.db_config.is_complete:
        message = f"Database config is incomplete: {config.db_config_path}"
        if config.require_db:
            raise RuntimeError(message)
        LOGGER.warning("%s. Falling back to local archive.", message)
        return
    LOGGER.info("Testing PostgreSQL connection from %s", config.db_config_path)
    test_database_connection(config.db_config)


def process_report(source: Path, config: ServiceConfig) -> None:
    LOGGER.info("Processing %s", source)
    driver_overrides = load_driver_mapping(config.mapping_path)
    records = load_single_report(source)
    result = analyze_records(
        records,
        driver_overrides=driver_overrides,
        minimum_distance=config.minimum_distance,
    )

    if config.db_config.is_complete:
        remote_id = save_database_archive(config.db_config, records, source.name)
        LOGGER.info("Saved archive in PostgreSQL with id=%s", remote_id)
    elif config.require_db:
        raise RuntimeError(f"Database config is required but incomplete: {config.db_config_path}")
    else:
        archive_name = _timestamped_name(source, ".json")
        archive_path = _unique_path(config.archive_dir, archive_name)
        save_archive_report(archive_path, records, source.name)
        LOGGER.info("Saved local archive %s", archive_path)

    xlsx_name = _timestamped_name(source, "_raport.xlsx")
    xlsx_path = _unique_path(config.output_dir, xlsx_name)
    export_xlsx(result, xlsx_path)
    LOGGER.info("Saved XLSX report %s", xlsx_path)

    if config.export_csv:
        csv_name = _timestamped_name(source, "_raport.csv")
        csv_path = _unique_path(config.output_dir, csv_name)
        export_csv(result, csv_path)
        LOGGER.info("Saved CSV report %s", csv_path)

    processed_path = _unique_path(config.processed_dir, source.name)
    shutil.move(str(source), processed_path)
    LOGGER.info("Moved source to %s", processed_path)


def _handle_failure(source: Path, config: ServiceConfig, error: Exception) -> None:
    LOGGER.exception("Failed to process %s: %s", source, error)
    failed_path = _unique_path(config.failed_dir, source.name)
    try:
        shutil.move(str(source), failed_path)
        error_path = failed_path.with_suffix(f"{failed_path.suffix}.error.txt")
        error_path.write_text(str(error), encoding="utf-8")
        LOGGER.info("Moved failed source to %s", failed_path)
    except OSError:
        LOGGER.exception("Could not move failed source %s", source)


def run_once(config: ServiceConfig, file_path: Path | None = None) -> int:
    _ensure_directories(config)
    reports = [file_path] if file_path else _discover_reports(config.input_dir)
    if not reports:
        LOGGER.info("No XLSX files to process in %s", config.input_dir)
        return 0

    failures = 0
    for report in reports:
        try:
            process_report(report, config)
        except Exception as exc:
            failures += 1
            _handle_failure(report, config, exc)
    return failures


def run_watch(config: ServiceConfig) -> int:
    _ensure_directories(config)
    LOGGER.info(
        "Watching %s every %ss. Output: %s",
        config.input_dir,
        config.poll_interval,
        config.output_dir,
    )
    while not STOP_REQUESTED:
        run_once(config)
        for _ in range(config.poll_interval):
            if STOP_REQUESTED:
                break
            time.sleep(1)
    LOGGER.info("Service stopped")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fuel Insight headless service")
    parser.add_argument("--watch", action="store_true", help="Run forever and poll input directory")
    parser.add_argument("--file", type=Path, help="Process a single XLSX file")
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--processed-dir", type=Path)
    parser.add_argument("--failed-dir", type=Path)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--mapping-path", type=Path)
    parser.add_argument("--db-config", type=Path)
    parser.add_argument(
        "--minimum-distance",
        type=float,
        default=env_float("FUEL_INSIGHT_MIN_DISTANCE", 100.0),
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=env_int("FUEL_INSIGHT_POLL_INTERVAL", 60),
    )
    parser.add_argument(
        "--require-db",
        action=argparse.BooleanOptionalAction,
        default=env_bool("FUEL_INSIGHT_REQUIRE_DB", False),
    )
    parser.add_argument(
        "--export-csv",
        action=argparse.BooleanOptionalAction,
        default=env_bool("FUEL_INSIGHT_EXPORT_CSV", True),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _signal_stop)
    signal.signal(signal.SIGINT, _signal_stop)
    args = parse_args(argv)
    config = _build_config(args)
    _validate_database(config)
    if args.watch:
        return run_watch(config)
    return run_once(config, args.file)


if __name__ == "__main__":
    raise SystemExit(main())
