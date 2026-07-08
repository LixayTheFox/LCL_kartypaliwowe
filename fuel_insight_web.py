from __future__ import annotations

import argparse
import html
import mimetypes
import re
import shutil
import signal
import threading
from dataclasses import dataclass
from datetime import date, datetime
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from archive_database import (
    list_database_archives,
    load_database_archive,
    load_database_config,
    save_database_archive,
)
from fuel_analysis import (
    AnalysisResult,
    ArchivedReportInfo,
    FuelRecord,
    analyze_records,
    export_csv,
    export_xlsx,
    list_archive_reports,
    load_archive_report,
    load_driver_mapping,
    load_single_report,
    save_archive_report,
)
from fuel_insight_service import ServiceConfig, _build_config, _ensure_directories, _unique_path
from runtime_config import (
    WEB_HOST,
    WEB_MAX_UPLOAD_MB,
    WEB_PORT,
    configure_logging,
    env_bool,
    env_float,
    env_int,
)


LOGGER = configure_logging("fuel_insight.web")


@dataclass(slots=True)
class Upload:
    filename: str
    content: bytes


@dataclass(slots=True)
class FileEntry:
    name: str
    size: int
    modified_at: datetime


@dataclass(slots=True)
class ReportView:
    source_label: str
    result: AnalysisResult
    records: list[FuelRecord]
    generated_at: datetime
    archive_note: str = ""
    from_archive: bool = False


@dataclass(slots=True)
class ComparisonView:
    left_label: str
    right_label: str
    left_result: AnalysisResult
    right_result: AnalysisResult
    generated_at: datetime


@dataclass(slots=True)
class ArchiveOption:
    key: str
    label: str
    entry: ArchivedReportInfo


@dataclass(slots=True)
class WebState:
    config: ServiceConfig
    lock: object
    started_at: datetime
    max_upload_bytes: int
    current_report: ReportView | None = None
    current_comparison: ComparisonView | None = None


class FuelInsightServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, state: WebState):
        super().__init__(server_address, handler_class)
        self.state = state


class FuelInsightHandler(BaseHTTPRequestHandler):
    server: FuelInsightServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/":
            self._send_html(_render_index(self.server.state, query))
            return
        if parsed.path == "/compare":
            self._send_html(_render_compare_page(self.server.state, query))
            return
        if parsed.path == "/inbox":
            self._send_html(_render_inbox_page(self.server.state, query))
            return
        if parsed.path == "/exports":
            self._send_html(_render_exports_page(self.server.state, query))
            return
        if parsed.path == "/health":
            self._send_bytes(b'{"status":"ok"}\n', "application/json; charset=utf-8")
            return
        if parsed.path == "/archive":
            if query.get("key"):
                self._handle_archive(query)
            else:
                self._send_html(_render_archive_page(self.server.state, query))
            return
        if parsed.path == "/export":
            self._handle_export(query)
            return
        if parsed.path == "/download":
            self._handle_download(query)
            return
        self.send_error(404, "Nie znaleziono strony")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            self._handle_upload()
            return
        if parsed.path == "/analyze-inbox":
            self._handle_analyze_inbox()
            return
        if parsed.path == "/compare":
            self._handle_compare()
            return
        self.send_error(404, "Nie znaleziono strony")

    def log_message(self, fmt: str, *args) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _send_html(self, body: str, status: int = 200) -> None:
        self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8", status)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(
        self,
        message: str = "",
        kind: str = "ok",
        anchor: str = "raport",
        path: str = "/",
    ) -> None:
        params = {}
        if message:
            params["message"] = message
            params["kind"] = kind
        location = path
        if params:
            location += "?" + urlencode(params)
        if anchor:
            location += f"#{anchor}"
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _handle_upload(self) -> None:
        state = self.server.state
        try:
            length = _content_length(self.headers.get("Content-Length"))
            if length <= 0:
                raise ValueError("Brak pliku w formularzu.")
            if length > state.max_upload_bytes:
                limit_mb = max(1, state.max_upload_bytes // (1024 * 1024))
                raise ValueError(f"Plik jest za duzy. Limit: {limit_mb} MB.")

            body = self.rfile.read(length)
            upload = _extract_upload(body, self.headers.get("Content-Type", ""))
            filename = _safe_filename(upload.filename)
            state.config.input_dir.mkdir(parents=True, exist_ok=True)
            source_path = _unique_path(state.config.input_dir, filename)
            source_path.write_bytes(upload.content)

            with state.lock:
                state.current_report = _analyze_file(source_path, state.config)
            self._redirect("Raport zostal wczytany, przeliczony i zapisany w archiwum.")
        except Exception as exc:
            LOGGER.exception("Upload failed: %s", exc)
            self._redirect(str(exc), "error", anchor="upload")

    def _handle_analyze_inbox(self) -> None:
        state = self.server.state
        try:
            form = _read_form(self)
            filename = _safe_filename(form.get("file", [""])[0])
            source_path = state.config.input_dir / filename
            if not source_path.is_file():
                raise ValueError("Nie znaleziono pliku w inbox.")
            with state.lock:
                state.current_report = _analyze_file(source_path, state.config)
            self._redirect("Plik z inbox zostal wczytany do raportu.")
        except Exception as exc:
            LOGGER.exception("Inbox analysis failed: %s", exc)
            self._redirect(str(exc), "error", anchor="inbox", path="/inbox")

    def _handle_compare(self) -> None:
        state = self.server.state
        try:
            length = _content_length(self.headers.get("Content-Length"))
            if length <= 0:
                raise ValueError("Wybierz dwa pliki XLSX do porownania.")
            if length > state.max_upload_bytes * 2:
                limit_mb = max(1, (state.max_upload_bytes * 2) // (1024 * 1024))
                raise ValueError(f"Pliki sa za duze. Limit laczny: {limit_mb} MB.")

            body = self.rfile.read(length)
            uploads = _extract_named_uploads(
                body,
                self.headers.get("Content-Type", ""),
                ("left_report", "right_report"),
            )
            with state.lock:
                state.current_comparison = _compare_uploads(
                    uploads["left_report"],
                    uploads["right_report"],
                    state.config,
                )
            self._redirect("Porownanie zostalo przygotowane.", path="/compare", anchor="wynik")
        except Exception as exc:
            LOGGER.exception("Compare failed: %s", exc)
            self._redirect(str(exc), "error", anchor="compare", path="/compare")
    def _handle_archive(self, query: dict[str, list[str]]) -> None:
        state = self.server.state
        key = (query.get("key") or [""])[0]
        try:
            with state.lock:
                metadata, records = _load_archive_by_key(state.config, key)
                result = analyze_records(
                    records,
                    driver_overrides=load_driver_mapping(state.config.mapping_path),
                    minimum_distance=state.config.minimum_distance,
                )
                source_label = str(metadata.get("source_file") or "archiwum")
                state.current_report = ReportView(
                    source_label=source_label,
                    result=result,
                    records=records,
                    generated_at=datetime.now(),
                    archive_note="Wczytano z archiwum.",
                    from_archive=True,
                )
            self._redirect("Archiwum zostalo wczytane.")
        except Exception as exc:
            LOGGER.exception("Archive load failed: %s", exc)
            self._redirect(str(exc), "error", anchor="archiwum")

    def _handle_export(self, query: dict[str, list[str]]) -> None:
        state = self.server.state
        fmt = (query.get("format") or ["xlsx"])[0].lower()
        try:
            with state.lock:
                view = state.current_report
                if view is None:
                    raise ValueError("Najpierw wczytaj raport albo archiwum.")
                state.config.output_dir.mkdir(parents=True, exist_ok=True)
                safe_source = _safe_stem(view.source_label)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                if fmt == "csv":
                    filename = f"{timestamp}_{safe_source}_ranking.csv"
                    output = _unique_path(state.config.output_dir, filename)
                    export_csv(view.result, output)
                elif fmt == "xlsx":
                    filename = f"{timestamp}_{safe_source}_raport.xlsx"
                    output = _unique_path(state.config.output_dir, filename)
                    export_xlsx(view.result, output)
                else:
                    raise ValueError("Nieznany format eksportu.")
            self._send_file(output, download_name=output.name)
        except Exception as exc:
            LOGGER.exception("Export failed: %s", exc)
            self._redirect(str(exc), "error", anchor="raport")

    def _handle_download(self, query: dict[str, list[str]]) -> None:
        raw_name = (query.get("file") or [""])[0]
        filename = Path(raw_name.replace("\\", "/")).name
        if not filename:
            self.send_error(400, "Brak nazwy pliku")
            return
        target = self.server.state.config.output_dir / filename
        if not target.is_file():
            self.send_error(404, "Nie znaleziono pliku")
            return
        self._send_file(target, download_name=target.name)

    def _send_file(self, target: Path, download_name: str) -> None:
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{_header_filename(download_name)}"')
        self.end_headers()
        with target.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)


def _content_length(raw: str | None) -> int:
    try:
        return int(raw or "0")
    except ValueError:
        return 0


def _extract_upload(body: bytes, content_type: str) -> Upload:
    if "multipart/form-data" not in content_type:
        raise ValueError("Formularz musi wyslac plik jako multipart/form-data.")
    header = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8", "replace")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise ValueError("Nie udalo sie odczytac formularza uploadu.")
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        if part.get_param("name", header="content-disposition") != "report":
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        if not filename:
            raise ValueError("Brak nazwy pliku.")
        if not content:
            raise ValueError("Wyslany plik jest pusty.")
        return Upload(filename=filename, content=content)
    raise ValueError("Nie znaleziono pola pliku 'report'.")



def _extract_named_uploads(
    body: bytes,
    content_type: str,
    field_names: tuple[str, ...],
) -> dict[str, Upload]:
    if "multipart/form-data" not in content_type:
        raise ValueError("Formularz musi wyslac pliki jako multipart/form-data.")
    header = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8", "replace")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise ValueError("Nie udalo sie odczytac formularza uploadu.")

    expected = set(field_names)
    uploads: dict[str, Upload] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        field_name = part.get_param("name", header="content-disposition")
        if field_name not in expected:
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True) or b""
        if not filename:
            raise ValueError("Brakuje nazwy jednego z plikow.")
        if not content:
            raise ValueError(f"Plik {filename} jest pusty.")
        uploads[str(field_name)] = Upload(filename=filename, content=content)

    missing = [name for name in field_names if name not in uploads]
    if missing:
        raise ValueError("Wybierz oba pliki XLSX do porownania.")
    return uploads
def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, list[str]]:
    length = _content_length(handler.headers.get("Content-Length"))
    body = handler.rfile.read(length).decode("utf-8", "replace") if length else ""
    return parse_qs(body)


def _safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    safe = "".join(char if char in allowed else "_" for char in name).strip("._")
    if not safe:
        raise ValueError("Nieprawidlowa nazwa pliku.")
    if Path(safe).suffix.lower() != ".xlsx":
        raise ValueError("Wgraj plik XLSX.")
    return safe


def _safe_stem(value: str) -> str:
    stem = Path(value.replace("\\", "/")).stem or "raport"
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "raport"
    return safe[:80]


def _header_filename(filename: str) -> str:
    return filename.replace("\\", "_").replace('"', "_")


def _period_label(date_from: date | None, date_to: date | None) -> str:
    if date_from and date_to:
        return f"{date_from:%d.%m.%Y} - {date_to:%d.%m.%Y}"
    return "brak dat"


def _format_number(value: float | int | None, decimals: int = 2) -> str:
    if value is None:
        return "-"
    text = f"{float(value):,.{decimals}f}"
    return text.replace(",", " ").replace(".", ",")


def _format_date(value: date | datetime | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y")


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _archive_path_for(config: ServiceConfig, source_label: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _unique_path(config.archive_dir, f"{timestamp}_{_safe_stem(source_label)}.json")


def _refresh_db_config(config: ServiceConfig) -> None:
    config.db_config = load_database_config(config.db_config_path)


def _archive_records(config: ServiceConfig, records: list[FuelRecord], source_label: str) -> str:
    _refresh_db_config(config)
    if config.db_config.is_complete:
        try:
            remote_id = save_database_archive(config.db_config, records, source_label)
            return f"Raport zapisano w PostgreSQL, id={remote_id}."
        except Exception as exc:
            if config.require_db:
                raise RuntimeError(f"Nie udalo sie zapisac archiwum w PostgreSQL: {exc}") from exc
            archive_path = _archive_path_for(config, source_label)
            save_archive_report(archive_path, records, source_label)
            return f"PostgreSQL niedostepny, zapisano lokalnie: {archive_path.name}."
    archive_path = _archive_path_for(config, source_label)
    save_archive_report(archive_path, records, source_label)
    return f"Raport zapisano w lokalnym archiwum: {archive_path.name}."


def _analyze_file(source_path: Path, config: ServiceConfig) -> ReportView:
    LOGGER.info("Analyzing %s", source_path)
    records = load_single_report(source_path)
    result = analyze_records(
        records,
        driver_overrides=load_driver_mapping(config.mapping_path),
        minimum_distance=config.minimum_distance,
    )
    archive_note = _archive_records(config, records, source_path.name)
    return ReportView(
        source_label=source_path.name,
        result=result,
        records=records,
        generated_at=datetime.now(),
        archive_note=archive_note,
    )



def _analyze_uploaded_for_compare(upload: Upload, config: ServiceConfig) -> tuple[str, AnalysisResult]:
    filename = _safe_filename(upload.filename)
    compare_dir = config.input_dir / "_compare"
    compare_dir.mkdir(parents=True, exist_ok=True)
    source_path = _unique_path(compare_dir, filename)
    source_path.write_bytes(upload.content)
    try:
        records = load_single_report(source_path)
    finally:
        source_path.unlink(missing_ok=True)
    result = analyze_records(
        records,
        driver_overrides=load_driver_mapping(config.mapping_path),
        minimum_distance=config.minimum_distance,
    )
    return filename, result


def _compare_uploads(left: Upload, right: Upload, config: ServiceConfig) -> ComparisonView:
    left_label, left_result = _analyze_uploaded_for_compare(left, config)
    right_label, right_result = _analyze_uploaded_for_compare(right, config)
    return ComparisonView(
        left_label=left_label,
        right_label=right_label,
        left_result=left_result,
        right_result=right_result,
        generated_at=datetime.now(),
    )


def _comparison_key(row) -> str:
    if row.vehicles:
        return "|".join(row.vehicles)
    return row.driver.casefold()


def _comparison_label(key: str, left_row, right_row) -> str:
    row = left_row or right_row
    if row is None:
        return key.replace("|", ", ")
    return f"{row.driver} / {row.vehicle_label}"


def _delta(current: float | int | None, previous: float | int | None) -> float:
    return float(current or 0) - float(previous or 0)


def _format_delta(value: float | int | None, decimals: int = 2) -> str:
    number = float(value or 0)
    sign = "+" if number > 0 else ""
    return sign + _format_number(number, decimals)
def _archive_label(entry: ArchivedReportInfo) -> str:
    imported = entry.imported_at.strftime("%d.%m.%Y %H:%M") if entry.imported_at else "bez daty"
    period = _period_label(entry.date_from, entry.date_to)
    storage = "DB" if entry.storage == "database" else "lokalnie"
    return f"[{storage}] {imported} | {period} | {entry.source_file} ({entry.record_count})"


def _archive_key(entry: ArchivedReportInfo) -> str:
    if entry.storage == "database" and entry.remote_id is not None:
        return f"db:{entry.remote_id}"
    if entry.path is not None:
        return f"local:{entry.path.name}"
    return ""


def _archive_options(config: ServiceConfig) -> list[ArchiveOption]:
    entries: list[ArchivedReportInfo] = []
    _refresh_db_config(config)
    if config.db_config.is_complete:
        try:
            entries.extend(list_database_archives(config.db_config))
        except Exception as exc:
            LOGGER.warning("Could not list database archives: %s", exc)
    entries.extend(list_archive_reports(config.archive_dir))

    options: list[ArchiveOption] = []
    seen: set[str] = set()
    for entry in entries:
        key = _archive_key(entry)
        if not key or key in seen:
            continue
        seen.add(key)
        options.append(ArchiveOption(key=key, label=_archive_label(entry), entry=entry))
    return options


def _load_archive_by_key(config: ServiceConfig, key: str) -> tuple[dict[str, object], list[FuelRecord]]:
    if key.startswith("db:"):
        remote_id = int(key.split(":", 1)[1])
        _refresh_db_config(config)
        if not config.db_config.is_complete:
            raise ValueError("Konfiguracja bazy jest niepelna.")
        return load_database_archive(config.db_config, remote_id)
    if key.startswith("local:"):
        filename = Path(key.split(":", 1)[1].replace("\\", "/")).name
        if not filename.endswith(".json"):
            raise ValueError("Nieprawidlowy wpis archiwum.")
        return load_archive_report(config.archive_dir / filename)
    raise ValueError("Nie wybrano archiwum.")


def _list_files(directory: Path, pattern: str = "*", limit: int = 30) -> list[FileEntry]:
    if not directory.exists():
        return []
    entries: list[FileEntry] = []
    for path in directory.glob(pattern):
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append(FileEntry(path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime)))
    entries.sort(key=lambda entry: entry.modified_at, reverse=True)
    return entries[:limit]


def _database_summary(config: ServiceConfig) -> str:
    db_config = load_database_config(config.db_config_path)
    if not db_config.is_complete:
        return "lokalne archiwum"
    return f"{db_config.host}:{db_config.port}/{db_config.database} jako {db_config.username}"


def _message_box(query: dict[str, list[str]]) -> str:
    message = (query.get("message") or [""])[0]
    if not message:
        return ""
    kind = (query.get("kind") or ["ok"])[0]
    css = "notice"
    if kind == "error":
        css += " error"
    elif kind == "warn":
        css += " warn"
    return f'<div class="{css}">{html.escape(message)}</div>'


def _kpi(label: str, value: str, tone: str = "") -> str:
    return (
        f'<div class="kpi {tone}">'
        f"<span>{html.escape(label)}</span>"
        f"<strong>{html.escape(value)}</strong>"
        "</div>"
    )


def _render_report(view: ReportView | None) -> str:
    if view is None:
        return """
        <section id="raport" class="empty-report">
          <h2>Raport</h2>
          <p>Wgraj plik XLSX albo wybierz pozycje z archiwum. W tym miejscu pojawi sie podsumowanie, ranking i pelna tabela.</p>
        </section>
        """

    result = view.result
    period = _period_label(result.date_from, result.date_to)
    average = f"{_format_number(result.average_consumption)} l/100 km" if result.average_consumption is not None else "-"
    worst = "Brak danych do rankingu"
    if result.worst and result.worst.consumption is not None:
        worst = f"{result.worst.driver} | {_format_number(result.worst.consumption)} l/100 km"
    warnings = "".join(f"<li>{html.escape(warning)}</li>" for warning in result.warnings)
    warning_block = f'<ul class="warnings">{warnings}</ul>' if warnings else ""

    return f"""
    <section id="raport" class="report-head">
      <div>
        <h2>{html.escape(view.source_label)}</h2>
        <p>Okres: {html.escape(period)} | Transakcje: {len(view.records)} | W rankingu: {result.ranked_count}</p>
        <p>{html.escape(view.archive_note)}</p>
      </div>
      <div class="actions">
        <a class="button" href="/export?format=xlsx">Eksport XLSX</a>
        <a class="button secondary" href="/export?format=csv">Eksport CSV</a>
      </div>
    </section>
    {warning_block}
    <section class="kpi-grid">
      {_kpi("Kierowcy / pojazdy", str(len(result.rows)), "blue")}
      {_kpi("Paliwo razem", f"{_format_number(result.total_fuel)} l", "green")}
      {_kpi("Dystans", f"{_format_number(result.total_distance, 1)} km", "violet")}
      {_kpi("Srednie spalanie", average, "amber")}
      {_kpi("Najgorszy wynik", worst, "red")}
    </section>
    <section>
      <h2>Ranking i podsumowanie</h2>
      {_result_table(result)}
    </section>
    <section>
      <h2>Transakcje</h2>
      {_transactions_table(view.records, result)}
    </section>
    """


def _result_table(result: AnalysisResult) -> str:
    if not result.rows:
        return '<p class="empty">Brak wynikow.</p>'
    rows = []
    for row in result.rows:
        classes = []
        if row.rank == 1:
            classes.append("worst")
        if row.rank is None:
            classes.append("unranked")
        class_attr = f' class="{" ".join(classes)}"' if classes else ""
        rows.append(
            f"<tr{class_attr}>"
            f"<td>{html.escape(str(row.rank or '-'))}</td>"
            f"<td>{html.escape(row.driver)}</td>"
            f"<td>{html.escape(row.vehicle_label)}</td>"
            f"<td>{row.transactions}</td>"
            f"<td>{_format_number(row.diesel_total)}</td>"
            f"<td>{_format_number(row.gasoline_liters)}</td>"
            f"<td>{_format_number(row.fuel_total)}</td>"
            f"<td>{_format_number(row.adblue_liters)}</td>"
            f"<td>{_format_number(row.distance_km, 1)}</td>"
            f"<td>{_format_number(row.consumption)}</td>"
            f"<td>{_format_number(row.cost_eur)}</td>"
            f"<td>{_format_number(row.cost_pln)}</td>"
            f"<td>{html.escape(row.status)}</td>"
            "</tr>"
        )
    headers = [
        "Ranking",
        "Kierowca",
        "Pojazdy",
        "Transakcje",
        "Diesel [l]",
        "Benzyna [l]",
        "Paliwo [l]",
        "AdBlue [l]",
        "Dystans [km]",
        "Spalanie [l/100 km]",
        "Koszt [EUR]",
        "Koszt [PLN]",
        "Status",
    ]
    return _table(headers, rows, "wide-table")


def _transactions_table(records: list[FuelRecord], result: AnalysisResult) -> str:
    if not records:
        return '<p class="empty">Brak transakcji.</p>'
    vehicle_to_driver = {vehicle: row.driver for row in result.rows for vehicle in row.vehicles}
    rows = []
    for record in sorted(records, key=lambda item: (item.transaction_date or date.min, item.vehicle, item.product)):
        rows.append(
            "<tr>"
            f"<td>{html.escape(_format_date(record.transaction_date))}</td>"
            f"<td>{html.escape(vehicle_to_driver.get(record.vehicle, record.driver) or '-')}</td>"
            f"<td>{html.escape(record.vehicle)}</td>"
            f"<td>{html.escape(record.product)}</td>"
            f"<td>{_format_number(record.liters)}</td>"
            f"<td>{_format_number(record.amount)}</td>"
            f"<td>{html.escape(record.currency)}</td>"
            f"<td>{_format_number(record.odometer, 1)}</td>"
            f"<td>{html.escape(record.provider)}</td>"
            f"<td>{html.escape(record.station)}</td>"
            "</tr>"
        )
    headers = ["Data", "Kierowca", "Pojazd", "Produkt", "Ilosc [l]", "Kwota", "Waluta", "Przebieg", "Dostawca", "Stacja"]
    return _table(headers, rows, "wide-table")


def _table(headers: list[str], rows: list[str], css_class: str = "") -> str:
    header_cells = []
    filter_cells = []
    for index, header in enumerate(headers):
        escaped_header = html.escape(header)
        escaped_label = html.escape(header, quote=True) or f"Kolumna {index + 1}"
        header_cells.append(
            "<th>"
            f'<button type="button" class="sort-button" data-column="{index}" aria-label="Sortuj: {escaped_label}">'
            f"<span>{escaped_header}</span>"
            '<span class="sort-marker" aria-hidden="true"></span>'
            "</button>"
            "</th>"
        )
        filter_cells.append(
            "<th>"
            f'<input type="search" class="column-filter" data-column="{index}" '
            f'aria-label="Filtr: {escaped_label}" autocomplete="off">'
            "</th>"
        )
    table_class = " ".join(part for part in (css_class, "interactive-table") if part)
    return (
        f'<div class="table-scroll"><table class="{html.escape(table_class, quote=True)}">'
        f"<thead><tr>{''.join(header_cells)}</tr><tr class=\"filter-row\">{''.join(filter_cells)}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table></div>"
    )


def _render_archive(config: ServiceConfig) -> str:
    options = _archive_options(config)
    if not options:
        return '<p class="empty">Brak zapisanych raportow.</p>'
    items = []
    for option in options[:80]:
        href = "/archive?" + urlencode({"key": option.key})
        items.append(
            "<li>"
            f'<a href="{href}">{html.escape(option.label)}</a>'
            "</li>"
        )
    return f'<ul class="archive-list">{"".join(items)}</ul>'


def _render_inbox(config: ServiceConfig) -> str:
    files = _list_files(config.input_dir, "*.xlsx", 30)
    if not files:
        return '<p class="empty">Brak plikow XLSX w inbox.</p>'
    rows = []
    for entry in files:
        rows.append(
            "<tr>"
            f"<td>{html.escape(entry.name)}</td>"
            f"<td>{html.escape(_format_size(entry.size))}</td>"
            f"<td>{html.escape(entry.modified_at.strftime('%Y-%m-%d %H:%M:%S'))}</td>"
            "<td>"
            '<form method="post" action="/analyze-inbox">'
            f'<input type="hidden" name="file" value="{html.escape(entry.name)}">'
            '<button class="small" type="submit">Wczytaj</button>'
            "</form>"
            "</td>"
            "</tr>"
        )
    return _table(["Plik", "Rozmiar", "Data", ""], rows)


def _render_outputs(config: ServiceConfig) -> str:
    files = _list_files(config.output_dir, "*", 30)
    if not files:
        return '<p class="empty">Brak eksportow.</p>'
    rows = []
    for entry in files:
        href = "/download?" + urlencode({"file": entry.name})
        rows.append(
            "<tr>"
            f"<td>{html.escape(entry.name)}</td>"
            f"<td>{html.escape(_format_size(entry.size))}</td>"
            f"<td>{html.escape(entry.modified_at.strftime('%Y-%m-%d %H:%M:%S'))}</td>"
            f'<td><a class="small-button" href="{href}">Pobierz</a></td>'
            "</tr>"
        )
    return _table(["Plik", "Rozmiar", "Data", ""], rows)


def _nav(active: str) -> str:
    items = [
        ("report", "/", "Raport"),
        ("compare", "/compare", "Porownanie"),
        ("archive", "/archive", "Archiwum"),
        ("inbox", "/inbox", "Inbox"),
        ("exports", "/exports", "Eksporty"),
    ]
    links = []
    for key, href, label in items:
        css = ' class="active"' if key == active else ""
        links.append(f'<a{css} href="{href}">{html.escape(label)}</a>')
    return "".join(links)


def _render_page(title: str, active: str, query: dict[str, list[str]], body: str) -> str:
    return f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fuel Insight - {html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f4f7fb;
      --surface: #ffffff;
      --navy: #14213d;
      --blue: #2563eb;
      --blue-dark: #1d4ed8;
      --text: #182230;
      --muted: #667085;
      --line: #dce3ec;
      --green: #13795b;
      --red: #c62828;
      --amber: #b45309;
      --violet: #7c3aed;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); font-family: Arial, Helvetica, sans-serif; }}
    header {{ background: var(--navy); color: #fff; padding: 18px clamp(16px, 4vw, 40px); }}
    header h1 {{ margin: 0; font-size: 28px; letter-spacing: 0; }}
    header p {{ margin: 5px 0 0; color: #b9c4d6; }}
    nav {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 16px; }}
    nav a {{ color: #fff; text-decoration: none; border: 1px solid rgba(255,255,255,.25); border-radius: 6px; padding: 7px 10px; }}
    nav a.active {{ background: #fff; color: var(--navy); border-color: #fff; }}
    main {{ max-width: 1480px; margin: 0 auto; padding: 18px clamp(12px, 3vw, 24px) 48px; }}
    section {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; letter-spacing: 0; }}
    p {{ margin: 0 0 8px; color: var(--muted); }}
    .notice {{ border: 1px solid #b8e2c8; border-left: 4px solid var(--green); background: #f0fbf4; border-radius: 6px; padding: 12px 14px; margin-bottom: 16px; }}
    .notice.warn {{ border-color: #f1d18a; border-left-color: var(--amber); background: #fff8e8; }}
    .notice.error {{ border-color: #f1b8b2; border-left-color: var(--red); background: #fff1f0; }}
    .upload {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; }}
    .file-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .file-field {{ display: grid; gap: 6px; }}
    .file-field label {{ color: var(--muted); font-size: 13px; font-weight: 700; }}
    input[type="file"] {{ width: 100%; border: 1px dashed var(--line); border-radius: 6px; padding: 14px; background: #fbfcfe; }}
    button, .button, .small-button {{ appearance: none; border: 0; border-radius: 6px; background: var(--blue); color: #fff; cursor: pointer; display: inline-block; font: inherit; padding: 10px 14px; text-decoration: none; }}
    button:hover, .button:hover, .small-button:hover {{ background: var(--blue-dark); }}
    .sort-button {{ appearance: none; border: 0; border-radius: 0; background: transparent; color: inherit; cursor: pointer; display: flex; align-items: center; gap: 6px; justify-content: space-between; width: 100%; padding: 0; text-align: left; text-transform: inherit; font: inherit; }}
    .sort-button:hover {{ background: transparent; color: inherit; text-decoration: underline; }}
    .sort-marker {{ color: #cbd5e1; font-size: 11px; min-width: 22px; text-align: right; text-transform: none; }}
    .button.secondary {{ background: #334155; }}
    .small, .small-button {{ padding: 7px 10px; font-size: 13px; }}
    .report-head {{ display: flex; justify-content: space-between; gap: 14px; align-items: start; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(5, minmax(160px, 1fr)); gap: 12px; background: transparent; border: 0; padding: 0; }}
    .kpi {{ background: var(--surface); border: 1px solid var(--line); border-left: 5px solid var(--blue); border-radius: 8px; padding: 13px; min-width: 0; }}
    .kpi span {{ display: block; color: var(--muted); font-size: 12px; text-transform: uppercase; margin-bottom: 6px; }}
    .kpi strong {{ display: block; font-size: 19px; overflow-wrap: anywhere; }}
    .kpi.green {{ border-left-color: var(--green); }}
    .kpi.red {{ border-left-color: var(--red); }}
    .kpi.amber {{ border-left-color: var(--amber); }}
    .kpi.violet {{ border-left-color: var(--violet); }}
    .warnings {{ margin: 0 0 16px; border: 1px solid #f1d18a; border-radius: 8px; background: #fff8e8; padding: 12px 18px 12px 34px; color: #7a4b00; }}
    .table-scroll {{ overflow: auto; max-height: 680px; border: 1px solid var(--line); border-radius: 6px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 9px 8px; text-align: left; vertical-align: top; white-space: nowrap; }}
    th {{ position: sticky; top: 0; z-index: 1; background: var(--navy); color: #fff; font-size: 12px; text-transform: uppercase; }}
    thead tr:first-child th {{ z-index: 3; }}
    .filter-row th {{ top: 36px; z-index: 2; background: #eef3f9; color: var(--text); padding: 6px; }}
    .column-filter {{ box-sizing: border-box; width: 100%; min-width: 92px; border: 1px solid var(--line); border-radius: 4px; padding: 6px 7px; background: #fff; color: var(--text); font: inherit; font-size: 12px; }}
    tbody tr:nth-child(even) {{ background: #f8fafc; }}
    tr.worst {{ background: #fdecec !important; color: var(--red); font-weight: 700; }}
    tr.unranked {{ color: #7b8794; }}
    .wide-table td:nth-child(2), .wide-table td:nth-child(3) {{ white-space: normal; min-width: 150px; }}
    .archive-list {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }}
    .archive-list a {{ display: block; text-decoration: none; color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 10px; background: #fbfcfe; }}
    .archive-list a:hover {{ border-color: var(--blue); }}
    .empty, .empty-report p {{ color: var(--muted); }}
    @media (max-width: 980px) {{
      .kpi-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .upload, .file-grid {{ grid-template-columns: 1fr; }}
      .report-head {{ display: block; }}
      .actions {{ justify-content: flex-start; margin-top: 10px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Fuel Insight</h1>
    <p>Raport efektywnosci kierowcow i pojazdow</p>
    <nav>{_nav(active)}</nav>
  </header>
  <main>
    {_message_box(query)}
    {body}
  </main>
  <script>
    (() => {{
      function cellText(row, index) {{
        const cell = row.cells[index];
        return cell ? cell.textContent.trim() : "";
      }}

      function numberValue(text) {{
        const normalized = text
          .replace(/\\s/g, "")
          .replace(",", ".")
          .replace(/[^0-9.+-]/g, "");
        if (!normalized || normalized === "-" || normalized === "+") {{
          return NaN;
        }}
        return Number(normalized);
      }}

      function applyFilters(table) {{
        const filters = Array.from(table.querySelectorAll(".column-filter"));
        const body = table.tBodies[0];
        if (!body) {{
          return;
        }}
        Array.from(body.rows).forEach((row) => {{
          const visible = filters.every((input) => {{
            const value = input.value.trim().toLowerCase();
            if (!value) {{
              return true;
            }}
            return cellText(row, Number(input.dataset.column)).toLowerCase().includes(value);
          }});
          row.hidden = !visible;
        }});
      }}

      function compareText(left, right) {{
        return left.localeCompare(right, "pl", {{ numeric: true, sensitivity: "base" }});
      }}

      document.querySelectorAll(".interactive-table").forEach((table) => {{
        const body = table.tBodies[0];
        if (!body) {{
          return;
        }}

        table.querySelectorAll(".column-filter").forEach((input) => {{
          input.addEventListener("input", () => applyFilters(table));
        }});

        table.querySelectorAll(".sort-button").forEach((button) => {{
          button.addEventListener("click", () => {{
            const column = Number(button.dataset.column);
            const currentColumn = table.dataset.sortColumn;
            const nextDirection = currentColumn === String(column) && table.dataset.sortDirection === "asc" ? "desc" : "asc";
            table.dataset.sortColumn = String(column);
            table.dataset.sortDirection = nextDirection;

            const rows = Array.from(body.rows);
            rows.sort((leftRow, rightRow) => {{
              const leftText = cellText(leftRow, column);
              const rightText = cellText(rightRow, column);
              const leftNumber = numberValue(leftText);
              const rightNumber = numberValue(rightText);
              let order;
              if (!Number.isNaN(leftNumber) && !Number.isNaN(rightNumber)) {{
                order = leftNumber - rightNumber;
              }} else {{
                order = compareText(leftText, rightText);
              }}
              return nextDirection === "asc" ? order : -order;
            }});

            rows.forEach((row) => body.appendChild(row));
            table.querySelectorAll(".sort-button").forEach((other) => {{
              other.removeAttribute("aria-sort");
              const marker = other.querySelector(".sort-marker");
              if (marker) {{
                marker.textContent = "";
              }}
            }});
            button.setAttribute("aria-sort", nextDirection === "asc" ? "ascending" : "descending");
            const marker = button.querySelector(".sort-marker");
            if (marker) {{
              marker.textContent = nextDirection === "asc" ? "A-Z" : "Z-A";
            }}
            applyFilters(table);
          }});
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


def _render_index(state: WebState, query: dict[str, list[str]]) -> str:
    with state.lock:
        current_report = state.current_report
    body = f"""
    <section id="upload">
      <h2>Wczytaj raport XLSX</h2>
      <form class="upload" action="/upload" method="post" enctype="multipart/form-data">
        <input type="file" name="report" accept=".xlsx" required>
        <button type="submit">Analizuj raport</button>
      </form>
    </section>
    {_render_report(current_report)}
    """
    return _render_page("Raport", "report", query, body)


def _render_archive_page(state: WebState, query: dict[str, list[str]]) -> str:
    body = f"""
    <section id="archiwum">
      <h2>Archiwum</h2>
      {_render_archive(state.config)}
    </section>
    """
    return _render_page("Archiwum", "archive", query, body)


def _render_inbox_page(state: WebState, query: dict[str, list[str]]) -> str:
    body = f"""
    <section id="inbox">
      <h2>Pliki w inbox</h2>
      {_render_inbox(state.config)}
    </section>
    """
    return _render_page("Inbox", "inbox", query, body)


def _render_exports_page(state: WebState, query: dict[str, list[str]]) -> str:
    body = f"""
    <section id="eksporty">
      <h2>Eksporty</h2>
      {_render_outputs(state.config)}
    </section>
    """
    return _render_page("Eksporty", "exports", query, body)


def _render_compare_page(state: WebState, query: dict[str, list[str]]) -> str:
    with state.lock:
        comparison = state.current_comparison
    body = f"""
    <section id="compare">
      <h2>Porownaj dwa raporty XLSX</h2>
      <form action="/compare" method="post" enctype="multipart/form-data">
        <div class="file-grid">
          <div class="file-field">
            <label>Raport bazowy</label>
            <input type="file" name="left_report" accept=".xlsx" required>
          </div>
          <div class="file-field">
            <label>Raport porownywany</label>
            <input type="file" name="right_report" accept=".xlsx" required>
          </div>
        </div>
        <p style="margin-top: 12px;"><button type="submit">Porownaj pliki</button></p>
      </form>
    </section>
    {_render_comparison(comparison)}
    """
    return _render_page("Porownanie", "compare", query, body)


def _render_comparison(view: ComparisonView | None) -> str:
    if view is None:
        return '<section id="wynik" class="empty-report"><h2>Wynik porownania</h2><p>Wybierz dwa pliki XLSX, zeby zobaczyc roznice w paliwie, dystansie, spalaniu i transakcjach.</p></section>'

    left = view.left_result
    right = view.right_result
    left_avg = left.average_consumption or 0
    right_avg = right.average_consumption or 0
    cards = f"""
    <section id="wynik" class="report-head">
      <div>
        <h2>{html.escape(view.left_label)} -> {html.escape(view.right_label)}</h2>
        <p>Wygenerowano: {html.escape(view.generated_at.strftime('%Y-%m-%d %H:%M:%S'))}</p>
      </div>
    </section>
    <section class="kpi-grid">
      {_kpi('Paliwo roznica', f"{_format_delta(_delta(right.total_fuel, left.total_fuel))} l", 'green')}
      {_kpi('Dystans roznica', f"{_format_delta(_delta(right.total_distance, left.total_distance), 1)} km", 'violet')}
      {_kpi('Spalanie roznica', f"{_format_delta(_delta(right_avg, left_avg))} l/100 km", 'amber')}
      {_kpi('Transakcje roznica', _format_delta(_delta(len(right.records), len(left.records)), 0), 'blue')}
      {_kpi('Pozycji w rankingu', f"{left.ranked_count} -> {right.ranked_count}", 'red')}
    </section>
    """
    left_rows = {_comparison_key(row): row for row in left.rows}
    right_rows = {_comparison_key(row): row for row in right.rows}
    keys = sorted(
        set(left_rows) | set(right_rows),
        key=lambda key: abs(_delta(getattr(right_rows.get(key), 'fuel_total', 0), getattr(left_rows.get(key), 'fuel_total', 0))),
        reverse=True,
    )
    rows = []
    for key in keys:
        left_row = left_rows.get(key)
        right_row = right_rows.get(key)
        left_consumption = left_row.consumption if left_row and left_row.consumption is not None else 0
        right_consumption = right_row.consumption if right_row and right_row.consumption is not None else 0
        rows.append(
            "<tr>"
            f"<td>{html.escape(_comparison_label(key, left_row, right_row))}</td>"
            f"<td>{_format_number(getattr(left_row, 'fuel_total', 0))}</td>"
            f"<td>{_format_number(getattr(right_row, 'fuel_total', 0))}</td>"
            f"<td>{_format_delta(_delta(getattr(right_row, 'fuel_total', 0), getattr(left_row, 'fuel_total', 0)))}</td>"
            f"<td>{_format_number(getattr(left_row, 'distance_km', 0), 1)}</td>"
            f"<td>{_format_number(getattr(right_row, 'distance_km', 0), 1)}</td>"
            f"<td>{_format_delta(_delta(getattr(right_row, 'distance_km', 0), getattr(left_row, 'distance_km', 0)), 1)}</td>"
            f"<td>{_format_number(left_consumption)}</td>"
            f"<td>{_format_number(right_consumption)}</td>"
            f"<td>{_format_delta(_delta(right_consumption, left_consumption))}</td>"
            f"<td>{int(getattr(left_row, 'transactions', 0))}</td>"
            f"<td>{int(getattr(right_row, 'transactions', 0))}</td>"
            f"<td>{_format_delta(_delta(getattr(right_row, 'transactions', 0), getattr(left_row, 'transactions', 0)), 0)}</td>"
            "</tr>"
        )
    headers = [
        "Kierowca / pojazd",
        "Paliwo A",
        "Paliwo B",
        "Delta paliwa",
        "Dystans A",
        "Dystans B",
        "Delta dystansu",
        "Spalanie A",
        "Spalanie B",
        "Delta spalania",
        "Trans. A",
        "Trans. B",
        "Delta trans.",
    ]
    return cards + f"<section><h2>Roznice per pozycja</h2>{_table(headers, rows, 'wide-table')}</section>"
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fuel Insight web report UI")
    parser.add_argument("--host", default=WEB_HOST)
    parser.add_argument("--port", type=int, default=WEB_PORT)
    parser.add_argument("--max-upload-mb", type=int, default=WEB_MAX_UPLOAD_MB)
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
    args = parse_args(argv)
    config = _build_config(args)
    _ensure_directories(config)
    state = WebState(
        config=config,
        lock=threading.RLock(),
        started_at=datetime.now(),
        max_upload_bytes=max(1, int(args.max_upload_mb)) * 1024 * 1024,
    )
    server = FuelInsightServer((args.host, int(args.port)), FuelInsightHandler, state)

    def _shutdown(_signum, _frame) -> None:
        LOGGER.info("Web stop requested")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    LOGGER.info("Fuel Insight report UI started on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        LOGGER.info("Fuel Insight report UI stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
