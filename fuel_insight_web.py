from __future__ import annotations

import argparse
import html
import mimetypes
import shutil
import signal
import threading
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from archive_database import load_database_config
from fuel_insight_service import (
    ServiceConfig,
    _build_config,
    _database_ready_for_watch,
    _ensure_directories,
    _handle_failure,
    _unique_path,
    process_report,
    run_once,
)
from runtime_config import (
    WEB_HOST,
    WEB_MAX_UPLOAD_MB,
    WEB_PORT,
    WEB_WATCH_INPUT,
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
class WebState:
    config: ServiceConfig
    service_lock: threading.Lock
    stop_event: threading.Event
    started_at: datetime
    watch_input: bool
    max_upload_bytes: int


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
        if parsed.path == "/":
            self._send_html(_render_index(self.server.state, parse_qs(parsed.query)))
            return
        if parsed.path == "/health":
            self._send_bytes(b'{"status":"ok"}\n', "application/json; charset=utf-8")
            return
        if parsed.path == "/download":
            self._handle_download(parse_qs(parsed.query))
            return
        self.send_error(404, "Nie znaleziono strony")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            self._handle_upload()
            return
        if parsed.path == "/process-inbox":
            self._handle_process_inbox()
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

    def _redirect(self, message: str, kind: str = "ok") -> None:
        location = "/?" + urlencode({"message": message, "kind": kind})
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
            target = _unique_path(state.config.input_dir, filename)
            target.write_bytes(upload.content)
            LOGGER.info("Uploaded %s to %s", upload.filename, target)

            with state.service_lock:
                if not _database_ready_for_watch(state.config):
                    self._redirect(
                        "Plik zapisany w inbox. Baza nie jest gotowa, wiec przetwarzanie poczeka.",
                        "warn",
                    )
                    return
                try:
                    process_report(target, state.config)
                except Exception as exc:
                    _handle_failure(target, state.config, exc)
                    raise

            self._redirect("Raport zostal przetworzony. Wynik jest w outbox.", "ok")
        except Exception as exc:
            LOGGER.exception("Upload failed: %s", exc)
            self._redirect(str(exc), "error")

    def _handle_process_inbox(self) -> None:
        state = self.server.state
        try:
            with state.service_lock:
                if not _database_ready_for_watch(state.config):
                    self._redirect("Baza nie jest gotowa, inbox zostal nietkniety.", "warn")
                    return
                failures = run_once(state.config)
            if failures:
                self._redirect(f"Inbox przetworzony, bledy: {failures}.", "warn")
            else:
                self._redirect("Inbox przetworzony bez bledow.", "ok")
        except Exception as exc:
            LOGGER.exception("Inbox processing failed: %s", exc)
            self._redirect(str(exc), "error")

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

        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{_header_filename(target.name)}"')
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


def _safe_filename(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    safe = "".join(char if char in allowed else "_" for char in name).strip("._")
    if not safe:
        raise ValueError("Nieprawidlowa nazwa pliku.")
    if Path(safe).suffix.lower() != ".xlsx":
        raise ValueError("Wgraj plik XLSX.")
    return safe


def _header_filename(filename: str) -> str:
    return filename.replace("\\", "_").replace('"', "_")


def _list_files(directory: Path, pattern: str = "*", limit: int = 30) -> list[FileEntry]:
    if not directory.exists():
        return []
    entries: list[FileEntry] = []
    for path in directory.glob(pattern):
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append(
            FileEntry(
                name=path.name,
                size=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime),
            )
        )
    entries.sort(key=lambda entry: entry.modified_at, reverse=True)
    return entries[:limit]


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _database_summary(config: ServiceConfig) -> str:
    db_config = load_database_config(config.db_config_path)
    if not db_config.is_complete:
        return "brak lub niepelny plik konfiguracji"
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


def _file_table(entries: list[FileEntry], allow_download: bool = False) -> str:
    if not entries:
        return '<p class="empty">Brak plikow.</p>'
    rows = []
    for entry in entries:
        name = html.escape(entry.name)
        modified = html.escape(entry.modified_at.strftime("%Y-%m-%d %H:%M:%S"))
        size = html.escape(_format_size(entry.size))
        action = ""
        if allow_download:
            href = "/download?" + urlencode({"file": entry.name})
            action = f'<a class="small-button" href="{href}">Pobierz</a>'
        rows.append(
            "<tr>"
            f"<td>{name}</td>"
            f"<td>{size}</td>"
            f"<td>{modified}</td>"
            f"<td>{action}</td>"
            "</tr>"
        )
    return (
        "<table>"
        "<thead><tr><th>Plik</th><th>Rozmiar</th><th>Data</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _render_index(state: WebState, query: dict[str, list[str]]) -> str:
    config = state.config
    output_files = _list_files(config.output_dir, "*", 50)
    inbox_files = _list_files(config.input_dir, "*.xlsx", 20)
    processed_files = _list_files(config.processed_dir, "*.xlsx", 20)
    failed_files = _list_files(config.failed_dir, "*", 20)
    started = html.escape(state.started_at.strftime("%Y-%m-%d %H:%M:%S"))
    watch_label = "wlaczony" if state.watch_input else "wylaczony"
    db_summary = html.escape(_database_summary(config))

    paths = {
        "Inbox": config.input_dir,
        "Outbox": config.output_dir,
        "Processed": config.processed_dir,
        "Failed": config.failed_dir,
        "DB config": config.db_config_path,
    }
    path_cards = "".join(
        f"<div><strong>{html.escape(label)}</strong><span>{html.escape(str(path))}</span></div>"
        for label, path in paths.items()
    )

    return f"""<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fuel Insight</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --surface: #ffffff;
      --text: #172033;
      --muted: #657089;
      --line: #dce2ea;
      --primary: #1456a0;
      --primary-dark: #0d3f78;
      --ok: #0f7a4c;
      --warn: #9a6700;
      --error: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      background: var(--surface);
      border-bottom: 1px solid var(--line);
      padding: 22px clamp(16px, 4vw, 48px);
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px clamp(16px, 4vw, 32px) 48px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 28px;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 0 0 14px;
      font-size: 18px;
      letter-spacing: 0;
    }}
    p {{ margin: 0; color: var(--muted); }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(320px, .85fr);
      gap: 18px;
      align-items: start;
    }}
    section {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 18px;
    }}
    .status {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .status div {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      min-width: 0;
    }}
    .status strong {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 5px;
      text-transform: uppercase;
    }}
    .status span {{
      display: block;
      overflow-wrap: anywhere;
      font-size: 14px;
    }}
    .notice {{
      margin-bottom: 18px;
      border: 1px solid #b8e2c8;
      border-left: 4px solid var(--ok);
      background: #f0fbf4;
      border-radius: 6px;
      padding: 12px 14px;
    }}
    .notice.warn {{
      border-color: #f1d18a;
      border-left-color: var(--warn);
      background: #fff8e8;
    }}
    .notice.error {{
      border-color: #f1b8b2;
      border-left-color: var(--error);
      background: #fff1f0;
    }}
    form.upload {{
      display: grid;
      gap: 12px;
    }}
    input[type="file"] {{
      width: 100%;
      border: 1px dashed var(--line);
      border-radius: 6px;
      padding: 18px;
      background: #fbfcfe;
    }}
    button, .small-button {{
      appearance: none;
      border: 0;
      border-radius: 6px;
      background: var(--primary);
      color: #fff;
      cursor: pointer;
      display: inline-block;
      font: inherit;
      padding: 10px 14px;
      text-decoration: none;
    }}
    button:hover, .small-button:hover {{
      background: var(--primary-dark);
    }}
    .secondary {{
      background: #e8edf5;
      color: var(--text);
    }}
    .secondary:hover {{
      background: #dce4ef;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      text-align: left;
      vertical-align: middle;
      overflow-wrap: anywhere;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
    }}
    td:last-child {{
      width: 92px;
      text-align: right;
    }}
    .empty {{
      color: var(--muted);
      padding: 8px 0;
    }}
    .stack {{
      display: grid;
      gap: 18px;
    }}
    .meta {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 14px;
    }}
    @media (max-width: 820px) {{
      .grid {{ grid-template-columns: 1fr; }}
      table {{ font-size: 13px; }}
      th:nth-child(2), td:nth-child(2) {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Fuel Insight</h1>
    <p>Panel do wgrywania raportow XLSX i pobierania wynikow z VM.</p>
  </header>
  <main>
    {_message_box(query)}
    <section>
      <h2>Status</h2>
      <div class="meta">
        <span>Start procesu: {started}</span>
        <span>Watcher inbox: {watch_label}</span>
        <span>Baza: {db_summary}</span>
      </div>
      <div class="status">{path_cards}</div>
    </section>
    <div class="grid">
      <div>
        <section>
          <h2>Wgraj raport XLSX</h2>
          <form class="upload" action="/upload" method="post" enctype="multipart/form-data">
            <input type="file" name="report" accept=".xlsx" required>
            <button type="submit">Przetworz raport</button>
          </form>
        </section>
        <section>
          <h2>Raporty wynikowe</h2>
          {_file_table(output_files, allow_download=True)}
        </section>
      </div>
      <aside class="stack">
        <section>
          <h2>Inbox</h2>
          <form action="/process-inbox" method="post">
            <button class="secondary" type="submit">Przetworz inbox teraz</button>
          </form>
          {_file_table(inbox_files)}
        </section>
        <section>
          <h2>Przetworzone</h2>
          {_file_table(processed_files)}
        </section>
        <section>
          <h2>Bledy</h2>
          {_file_table(failed_files)}
        </section>
      </aside>
    </div>
  </main>
</body>
</html>
"""


def _watch_loop(state: WebState) -> None:
    LOGGER.info("Background inbox watcher started")
    while not state.stop_event.is_set():
        try:
            with state.service_lock:
                if _database_ready_for_watch(state.config):
                    run_once(state.config)
        except Exception:
            LOGGER.exception("Background inbox processing failed")
        if state.stop_event.wait(state.config.poll_interval):
            break
    LOGGER.info("Background inbox watcher stopped")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fuel Insight web UI")
    parser.add_argument("--host", default=WEB_HOST)
    parser.add_argument("--port", type=int, default=WEB_PORT)
    parser.add_argument(
        "--watch-input",
        action=argparse.BooleanOptionalAction,
        default=WEB_WATCH_INPUT,
        help="Process XLSX files dropped into the input directory in the background",
    )
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
        service_lock=threading.Lock(),
        stop_event=threading.Event(),
        started_at=datetime.now(),
        watch_input=bool(args.watch_input),
        max_upload_bytes=max(1, int(args.max_upload_mb)) * 1024 * 1024,
    )

    watcher = None
    if state.watch_input:
        watcher = threading.Thread(target=_watch_loop, args=(state,), name="fuel-insight-watch", daemon=True)
        watcher.start()

    server = FuelInsightServer((args.host, int(args.port)), FuelInsightHandler, state)

    def _shutdown(_signum, _frame) -> None:
        LOGGER.info("Web stop requested")
        state.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    LOGGER.info("Fuel Insight web UI started on http://%s:%s", args.host, args.port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        state.stop_event.set()
        server.server_close()
        if watcher:
            watcher.join(timeout=5)
        LOGGER.info("Fuel Insight web UI stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
