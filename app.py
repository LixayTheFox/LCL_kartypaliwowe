from __future__ import annotations

import re
import sys
import threading
from datetime import datetime
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from archive_database import (
    DatabaseConfig,
    list_database_archives,
    load_database_archive,
    load_database_config,
    save_database_archive,
    save_database_config,
    test_database_connection,
)
from fuel_analysis import (
    AnalysisResult,
    ArchivedReportInfo,
    DriverSummary,
    analyze_records,
    export_csv,
    export_xlsx,
    list_archive_reports,
    load_archive_report,
    load_driver_mapping,
    load_single_report,
    save_archive_report,
    save_driver_mapping,
)
from runtime_config import ARCHIVE_DIR, DB_CONFIG_PATH, MAPPING_PATH, configure_logging


APP_TITLE = "Fuel Insight"
BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)

COLORS = {
    "background": "#F4F7FB",
    "surface": "#FFFFFF",
    "navy": "#14213D",
    "blue": "#2563EB",
    "blue_hover": "#1D4ED8",
    "text": "#182230",
    "muted": "#667085",
    "line": "#DCE3EC",
    "pale_blue": "#EAF2FF",
    "red": "#C62828",
    "pale_red": "#FDECEC",
    "green": "#13795B",
    "pale_green": "#E9F7F1",
    "gray": "#EEF1F5",
}


def format_number(value: float | None, decimals: int = 2) -> str:
    if value is None:
        return "-"
    text = f"{value:,.{decimals}f}"
    return text.replace(",", "\u00a0").replace(".", ",")


class FuelInsightApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} - analiza raportu paliwowego")
        self.geometry("1480x900")
        self.minsize(1120, 720)
        self.configure(bg=COLORS["background"])

        self.report_path = tk.StringVar()
        self.archive_selection = tk.StringVar()
        self.archive_location_text = tk.StringVar(value="Zapisane wcześniejsze raporty")
        self.minimum_distance = tk.StringVar(value="100")
        self.search_text = tk.StringVar()
        self.driver_name = tk.StringVar()
        self.status_text = tk.StringVar(value="Wybierz raport XLSX lub wczytaj dane z archiwum.")
        self.warning_text = tk.StringVar()
        self.db_config = load_database_config(DB_CONFIG_PATH)
        self.driver_overrides = load_driver_mapping(MAPPING_PATH)
        self.archive_entries: list[ArchivedReportInfo] = []
        self.archive_by_label: dict[str, ArchivedReportInfo] = {}

        self.records = []
        self.result: AnalysisResult | None = None
        self.iid_to_row: dict[str, DriverSummary] = {}
        self.sort_column = "rank"
        self.sort_reverse = False

        self._configure_styles()
        self._build_ui()
        self._detect_example_files()
        self._refresh_archive_list()
        self.search_text.trace_add("write", lambda *_: self._fill_table())

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            rowheight=34,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Treeview.Heading",
            background=COLORS["navy"],
            foreground="white",
            relief="flat",
            padding=(8, 9),
            font=("Segoe UI Semibold", 9),
        )
        style.map("Treeview.Heading", background=[("active", COLORS["blue"])])
        style.map(
            "Treeview",
            background=[("selected", COLORS["pale_blue"])],
            foreground=[("selected", COLORS["navy"])],
        )
        style.configure(
            "TEntry",
            fieldbackground="white",
            foreground=COLORS["text"],
            bordercolor=COLORS["line"],
            lightcolor=COLORS["line"],
            darkcolor=COLORS["line"],
            padding=7,
        )
        style.configure(
            "TSpinbox",
            fieldbackground="white",
            foreground=COLORS["text"],
            padding=6,
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=COLORS["blue"],
            troughcolor=COLORS["pale_blue"],
            borderwidth=0,
        )

    def _build_ui(self) -> None:
        header = tk.Frame(self, bg=COLORS["navy"], height=86)
        header.pack(fill="x")
        header.pack_propagate(False)

        brand = tk.Frame(header, bg=COLORS["navy"])
        brand.pack(side="left", padx=28, pady=17)
        logo = tk.Label(
            brand,
            text="FI",
            bg=COLORS["blue"],
            fg="white",
            font=("Segoe UI Semibold", 16),
            width=3,
            height=1,
        )
        logo.pack(side="left", padx=(0, 13))
        title_block = tk.Frame(brand, bg=COLORS["navy"])
        title_block.pack(side="left")
        tk.Label(
            title_block,
            text="Fuel Insight",
            bg=COLORS["navy"],
            fg="white",
            font=("Segoe UI Semibold", 19),
        ).pack(anchor="w")
        tk.Label(
            title_block,
            text="Raport efektywności kierowców i pojazdów",
            bg=COLORS["navy"],
            fg="#B9C4D6",
            font=("Segoe UI", 10),
        ).pack(anchor="w")

        self.export_xlsx_button = self._button(
            header,
            "Eksportuj XLSX",
            self.export_to_xlsx,
            bg=COLORS["green"],
            hover="#0F624A",
            state="disabled",
        )
        self.export_xlsx_button.pack(side="right", padx=(8, 28), pady=22)
        self.export_csv_button = self._button(
            header,
            "Eksportuj CSV",
            self.export_to_csv,
            bg="#334155",
            hover="#253248",
            state="disabled",
        )
        self.export_csv_button.pack(side="right", pady=22)
        self.database_button = self._button(
            header,
            "Baza danych",
            self.open_database_settings,
            bg="#475569",
            hover="#334155",
        )
        self.database_button.pack(side="right", padx=(8, 0), pady=22)

        content = tk.Frame(self, bg=COLORS["background"])
        content.pack(fill="both", expand=True, padx=24, pady=18)

        source_panel = tk.Frame(
            content,
            bg=COLORS["surface"],
            highlightbackground=COLORS["line"],
            highlightthickness=1,
        )
        source_panel.pack(fill="x")

        self._file_selector(
            source_panel,
            column=0,
            title="Raport XLSX",
            description="Pojazd, data, litry, koszt i przebieg",
            path_var=self.report_path,
            command=lambda: self.choose_file(self.report_path),
        )
        separator = tk.Frame(source_panel, bg=COLORS["line"], width=1)
        separator.grid(row=0, column=1, sticky="ns", pady=14)
        self._archive_selector(source_panel, column=2)

        controls = tk.Frame(source_panel, bg=COLORS["surface"])
        controls.grid(row=0, column=3, sticky="nsew", padx=18, pady=14)
        tk.Label(
            controls,
            text="Min. dystans do rankingu",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w")
        distance_row = tk.Frame(controls, bg=COLORS["surface"])
        distance_row.pack(fill="x", pady=(5, 9))
        ttk.Spinbox(
            distance_row,
            from_=0,
            to=10000,
            increment=50,
            width=9,
            textvariable=self.minimum_distance,
        ).pack(side="left")
        tk.Label(
            distance_row,
            text="km",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
        ).pack(side="left", padx=5)
        self.analyze_button = self._button(
            controls,
            "Analizuj dane",
            self.analyze_files,
            bg=COLORS["blue"],
            hover=COLORS["blue_hover"],
        )
        self.analyze_button.pack(fill="x")
        source_panel.grid_columnconfigure(0, weight=2)
        source_panel.grid_columnconfigure(2, weight=1)

        self.progress = ttk.Progressbar(
            content, mode="indeterminate", style="Horizontal.TProgressbar"
        )
        self.progress.pack(fill="x", pady=(10, 0))
        self.progress.pack_forget()

        self.warning_bar = tk.Label(
            content,
            textvariable=self.warning_text,
            bg="#FFF7E6",
            fg="#8A5A00",
            font=("Segoe UI", 9),
            anchor="w",
            padx=12,
            pady=8,
        )

        kpi_row = tk.Frame(content, bg=COLORS["background"])
        kpi_row.pack(fill="x", pady=(14, 14))
        self.kpi_people = self._kpi(kpi_row, "KIEROWCY / POJAZDY", "-", COLORS["blue"])
        self.kpi_fuel = self._kpi(kpi_row, "PALIWO RAZEM", "-", COLORS["green"])
        self.kpi_distance = self._kpi(kpi_row, "DYSTANS", "-", "#7C3AED")
        self.kpi_consumption = self._kpi(
            kpi_row, "ŚREDNIE SPALANIE", "-", "#B45309"
        )
        self.kpi_worst = self._kpi(
            kpi_row, "NAJGORSZY WYNIK", "-", COLORS["red"], wide=True
        )

        table_panel = tk.Frame(
            content,
            bg=COLORS["surface"],
            highlightbackground=COLORS["line"],
            highlightthickness=1,
        )
        table_panel.pack(fill="both", expand=True)

        table_toolbar = tk.Frame(table_panel, bg=COLORS["surface"])
        table_toolbar.pack(fill="x", padx=14, pady=12)
        tk.Label(
            table_toolbar,
            text="Wyniki",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 13),
        ).pack(side="left")
        tk.Label(
            table_toolbar,
            text="Najwyższe spalanie oznacza najgorszy wynik",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=12)
        search_wrap = tk.Frame(table_toolbar, bg=COLORS["surface"])
        search_wrap.pack(side="right")
        tk.Label(
            search_wrap,
            text="Szukaj:",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
        ).pack(side="left", padx=(0, 6))
        ttk.Entry(search_wrap, textvariable=self.search_text, width=28).pack(side="left")

        tree_wrap = tk.Frame(table_panel, bg=COLORS["surface"])
        tree_wrap.pack(fill="both", expand=True, padx=12)

        columns = (
            "rank",
            "driver",
            "vehicles",
            "transactions",
            "diesel_total",
            "gasoline",
            "fuel_total",
            "adblue",
            "distance",
            "consumption",
            "eur",
            "pln",
            "status",
        )
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings")
        labels = {
            "rank": "Ranking",
            "driver": "Kierowca",
            "vehicles": "Pojazdy",
            "transactions": "Transakcje",
            "diesel_total": "Diesel [l]",
            "gasoline": "Benzyna [l]",
            "fuel_total": "Paliwo [l]",
            "adblue": "AdBlue [l]",
            "distance": "Dystans [km]",
            "consumption": "Spalanie [l/100 km]",
            "eur": "Koszt [EUR]",
            "pln": "Koszt [PLN]",
            "status": "Status",
        }
        widths = {
            "rank": 72,
            "driver": 205,
            "vehicles": 165,
            "transactions": 90,
            "diesel_total": 110,
            "gasoline": 110,
            "fuel_total": 110,
            "adblue": 95,
            "distance": 115,
            "consumption": 145,
            "eur": 110,
            "pln": 110,
            "status": 140,
        }
        for column in columns:
            self.tree.heading(
                column,
                text=labels[column],
                command=lambda name=column: self.sort_by(name),
            )
            self.tree.column(
                column,
                width=widths[column],
                minwidth=60,
                anchor="w" if column in {"driver", "vehicles", "status"} else "center",
            )

        y_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        tree_wrap.grid_rowconfigure(0, weight=1)
        tree_wrap.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("worst", background=COLORS["pale_red"], foreground=COLORS["red"])
        self.tree.tag_configure("odd", background="#F8FAFC")
        self.tree.tag_configure("unranked", foreground="#7B8794")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

        mapping_bar = tk.Frame(table_panel, bg="#F8FAFC")
        mapping_bar.pack(fill="x", padx=12, pady=(10, 12))
        tk.Label(
            mapping_bar,
            text="Przypisz kierowcę do zaznaczonego pojazdu:",
            bg="#F8FAFC",
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", padx=(9, 8), pady=10)
        ttk.Entry(mapping_bar, textvariable=self.driver_name, width=30).pack(
            side="left", pady=8
        )
        self.assign_button = self._button(
            mapping_bar,
            "Zapisz przypisanie",
            self.assign_driver,
            bg=COLORS["blue"],
            hover=COLORS["blue_hover"],
            state="disabled",
            compact=True,
        )
        self.assign_button.pack(side="left", padx=8, pady=7)
        tk.Label(
            mapping_bar,
            text="Przypisania są zapamiętywane na tym komputerze.",
            bg="#F8FAFC",
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side="left", padx=4)

        status_bar = tk.Label(
            self,
            textvariable=self.status_text,
            bg="#E9EEF5",
            fg=COLORS["muted"],
            anchor="w",
            padx=24,
            pady=7,
            font=("Segoe UI", 9),
        )
        status_bar.pack(fill="x", side="bottom")

    def _button(
        self,
        parent,
        text: str,
        command,
        bg: str,
        hover: str,
        state: str = "normal",
        compact: bool = False,
    ) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg="white",
            activebackground=hover,
            activeforeground="white",
            disabledforeground="#D4D8DE",
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=12 if compact else 18,
            pady=6 if compact else 9,
            font=("Segoe UI Semibold", 9),
            state=state,
        )
        button.bind("<Enter>", lambda _: button.configure(bg=hover) if button["state"] == "normal" else None)
        button.bind("<Leave>", lambda _: button.configure(bg=bg) if button["state"] == "normal" else None)
        return button

    def _database_label(self) -> str:
        if not self.db_config.is_complete:
            return "Lokalne archiwum"
        return f"PostgreSQL: {self.db_config.host}/{self.db_config.database}"

    def open_database_settings(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Baza danych archiwum")
        dialog.configure(bg=COLORS["surface"])
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        host = tk.StringVar(value=self.db_config.host)
        port = tk.StringVar(value=str(self.db_config.port or 5432))
        database = tk.StringVar(value=self.db_config.database)
        username = tk.StringVar(value=self.db_config.username)
        password = tk.StringVar(value=self.db_config.password)
        sslmode = tk.StringVar(value=self.db_config.sslmode or "prefer")
        status = tk.StringVar(value="")

        body = tk.Frame(dialog, bg=COLORS["surface"], padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="Połączenie PostgreSQL",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 13),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        fields = [
            ("Adres serwera", host, False),
            ("Port", port, False),
            ("Nazwa bazy", database, False),
            ("Login", username, False),
            ("Hasło", password, True),
            ("SSL mode", sslmode, False),
        ]
        for row_index, (label, variable, secret) in enumerate(fields, start=1):
            tk.Label(
                body,
                text=label,
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                font=("Segoe UI", 9),
            ).grid(row=row_index, column=0, sticky="w", pady=5, padx=(0, 12))
            entry = ttk.Entry(body, textvariable=variable, width=36, show="*" if secret else "")
            entry.grid(row=row_index, column=1, sticky="ew", pady=5)

        tk.Label(
            body,
            textvariable=status,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            wraplength=360,
            justify="left",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))

        buttons = tk.Frame(body, bg=COLORS["surface"])
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(16, 0))

        def build_config() -> DatabaseConfig | None:
            try:
                parsed_port = int(port.get().strip() or "5432")
            except ValueError:
                messagebox.showwarning("Niepoprawny port", "Port musi być liczbą.", parent=dialog)
                return None
            config = DatabaseConfig(
                host=host.get().strip(),
                port=parsed_port,
                database=database.get().strip(),
                username=username.get().strip(),
                password=password.get(),
                sslmode=sslmode.get().strip() or "prefer",
            )
            if not config.is_complete:
                messagebox.showwarning(
                    "Brak danych",
                    "Uzupełnij adres serwera, nazwę bazy i login.",
                    parent=dialog,
                )
                return None
            return config

        def test_connection() -> None:
            config = build_config()
            if not config:
                return
            status.set("Testuję połączenie...")
            dialog.update_idletasks()
            try:
                test_database_connection(config)
            except Exception as exc:
                status.set("Połączenie nieudane.")
                messagebox.showerror("Błąd połączenia", str(exc), parent=dialog)
                return
            status.set("Połączenie działa. Tabela archiwum jest gotowa.")
            messagebox.showinfo("Połączenie OK", "Połączono z bazą PostgreSQL.", parent=dialog)

        def save_settings() -> None:
            config = build_config()
            if not config:
                return
            try:
                save_database_config(DB_CONFIG_PATH, config)
            except OSError as exc:
                messagebox.showerror("Nie zapisano ustawień", str(exc), parent=dialog)
                return
            self.db_config = config
            self._refresh_archive_list()
            self.status_text.set(f"Zapisano konfigurację bazy: {self._database_label()}.")
            dialog.destroy()

        self._button(
            buttons,
            "Testuj",
            test_connection,
            bg="#334155",
            hover="#253248",
            compact=True,
        ).pack(side="left", padx=(0, 8))
        self._button(
            buttons,
            "Zapisz",
            save_settings,
            bg=COLORS["blue"],
            hover=COLORS["blue_hover"],
            compact=True,
        ).pack(side="left", padx=(0, 8))
        self._button(
            buttons,
            "Anuluj",
            dialog.destroy,
            bg="#64748B",
            hover="#475569",
            compact=True,
        ).pack(side="left")

        body.grid_columnconfigure(1, weight=1)
        dialog.wait_visibility()
        dialog.focus_set()

    def _file_selector(
        self,
        parent,
        column: int,
        title: str,
        description: str,
        path_var: tk.StringVar,
        command,
    ) -> None:
        frame = tk.Frame(parent, bg=COLORS["surface"])
        frame.grid(row=0, column=column, sticky="nsew", padx=18, pady=14)
        tk.Label(
            frame,
            text=title,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w")
        tk.Label(
            frame,
            text=description,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(1, 7))
        row = tk.Frame(frame, bg=COLORS["surface"])
        row.pack(fill="x")
        label = tk.Label(
            row,
            textvariable=path_var,
            bg="#F8FAFC",
            fg=COLORS["muted"],
            anchor="w",
            padx=9,
            pady=7,
            font=("Segoe UI", 9),
        )
        label.pack(side="left", fill="x", expand=True)
        self._button(
            row,
            "Wybierz",
            command,
            bg="#334155",
            hover="#253248",
            compact=True,
        ).pack(side="left", padx=(8, 0))

    def _archive_selector(self, parent, column: int) -> None:
        frame = tk.Frame(parent, bg=COLORS["surface"])
        frame.grid(row=0, column=column, sticky="nsew", padx=18, pady=14)
        tk.Label(
            frame,
            text="Archiwum",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Segoe UI Semibold", 11),
        ).pack(anchor="w")
        tk.Label(
            frame,
            textvariable=self.archive_location_text,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(1, 7))
        row = tk.Frame(frame, bg=COLORS["surface"])
        row.pack(fill="x")
        self.archive_combo = ttk.Combobox(
            row,
            textvariable=self.archive_selection,
            state="disabled",
            width=38,
        )
        self.archive_combo.pack(side="left", fill="x", expand=True)
        self.archive_combo.bind("<<ComboboxSelected>>", lambda _event: self.load_selected_archive())
        self.load_archive_button = self._button(
            row,
            "Wczytaj",
            self.load_selected_archive,
            bg="#334155",
            hover="#253248",
            state="disabled",
            compact=True,
        )
        self.load_archive_button.pack(side="left", padx=(8, 0))


    def _kpi(
        self,
        parent,
        label: str,
        value: str,
        accent: str,
        wide: bool = False,
    ) -> tk.Label:
        card = tk.Frame(
            parent,
            bg=COLORS["surface"],
            highlightbackground=COLORS["line"],
            highlightthickness=1,
        )
        card.pack(side="left", fill="x", expand=2 if wide else 1, padx=(0, 10))
        tk.Frame(card, bg=accent, width=5).pack(side="left", fill="y")
        body = tk.Frame(card, bg=COLORS["surface"])
        body.pack(side="left", fill="both", expand=True, padx=13, pady=10)
        tk.Label(
            body,
            text=label,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w")
        value_label = tk.Label(
            body,
            text=value,
            bg=COLORS["surface"],
            fg=accent,
            font=("Segoe UI Semibold", 15 if not wide else 12),
            anchor="w",
        )
        value_label.pack(anchor="w", pady=(2, 0))
        return value_label

    def _detect_example_files(self) -> None:
        reports = sorted(BASE_DIR.glob("export_*.xlsx"))
        if reports:
            self.report_path.set(str(reports[0]))

    def choose_file(self, target: tk.StringVar) -> None:
        selected = filedialog.askopenfilename(
            title="Wybierz plik XLSX",
            initialdir=BASE_DIR,
            filetypes=[("Pliki Excel", "*.xlsx"), ("Wszystkie pliki", "*.*")],
        )
        if selected:
            target.set(selected)

    def _period_label(self, date_from, date_to) -> str:
        if date_from and date_to:
            return f"{date_from:%d.%m.%Y} - {date_to:%d.%m.%Y}"
        return "brak dat"

    def _archive_path_for(self, source: Path) -> Path:
        stem = re.sub(r"[^A-Za-z0-9_-]+", "_", source.stem).strip("_") or "raport"
        stem = stem[:60]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = ARCHIVE_DIR / f"{timestamp}_{stem}.json"
        counter = 2
        while candidate.exists():
            candidate = ARCHIVE_DIR / f"{timestamp}_{stem}_{counter}.json"
            counter += 1
        return candidate

    def _archive_label(self, entry: ArchivedReportInfo) -> str:
        imported = (
            entry.imported_at.strftime("%d.%m.%Y %H:%M")
            if entry.imported_at
            else "bez daty zapisu"
        )
        period = self._period_label(entry.date_from, entry.date_to)
        storage = "DB" if entry.storage == "database" else "lokalnie"
        return f"[{storage}] {imported} | {period} | {entry.source_file} ({entry.record_count})"

    def _local_archive_entries(self) -> list[ArchivedReportInfo]:
        self.archive_location_text.set("Lokalne archiwum")
        return list_archive_reports(ARCHIVE_DIR)

    def _refresh_archive_list(self) -> None:
        if self.db_config.is_complete:
            try:
                self.archive_entries = list_database_archives(self.db_config)
                self.archive_location_text.set(self._database_label())
            except Exception as exc:
                self.archive_entries = self._local_archive_entries()
                self.archive_location_text.set("Lokalne archiwum (baza niedostępna)")
                self.status_text.set(f"Nie udało się odczytać PostgreSQL: {exc}. Pokazuję lokalne archiwum.")
        else:
            self.archive_entries = self._local_archive_entries()

        self.archive_by_label = {}
        labels = []
        for entry in self.archive_entries:
            base_label = self._archive_label(entry)
            label = base_label
            duplicate = 2
            while label in self.archive_by_label:
                label = f"{base_label} [{duplicate}]"
                duplicate += 1
            self.archive_by_label[label] = entry
            labels.append(label)

        self.archive_combo.configure(values=labels)
        if labels:
            self.archive_combo.configure(state="readonly")
            self.load_archive_button.configure(state="normal")
            if self.archive_selection.get() not in self.archive_by_label:
                self.archive_selection.set(labels[0])
        else:
            self.archive_selection.set("")
            self.archive_combo.configure(state="disabled")
            self.load_archive_button.configure(state="disabled")

    def load_selected_archive(self) -> None:
        entry = self.archive_by_label.get(self.archive_selection.get())
        if not entry:
            messagebox.showwarning("Brak archiwum", "Nie wybrano zapisanego raportu.")
            return
        try:
            minimum_distance = self._minimum_distance_value()
        except ValueError as exc:
            messagebox.showwarning("Niepoprawna wartość", str(exc))
            return

        self._set_loading(True)
        self.status_text.set("Wczytuję dane archiwalne...")

        def worker() -> None:
            try:
                if entry.storage == "database":
                    if entry.remote_id is None:
                        raise ValueError("Archiwum z bazy nie ma identyfikatora.")
                    metadata, records = load_database_archive(self.db_config, entry.remote_id)
                    archive_remote_id = entry.remote_id
                    archive_path = None
                else:
                    if entry.path is None:
                        raise ValueError("Archiwum lokalne nie ma ścieżki pliku.")
                    metadata, records = load_archive_report(entry.path)
                    archive_remote_id = None
                    archive_path = entry.path
                result = analyze_records(
                    records,
                    driver_overrides=self.driver_overrides,
                    minimum_distance=minimum_distance,
                )
                source_label = str(metadata.get("source_file") or entry.source_file)
            except Exception as exc:
                self.after(0, lambda error=exc: self._analysis_failed(error))
                return
            self.after(
                0,
                lambda: self._analysis_ready(
                    records,
                    result,
                    source_label=source_label,
                    archive_path=archive_path,
                    archive_remote_id=archive_remote_id,
                    from_archive=True,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _minimum_distance_value(self) -> float:
        try:
            return max(0.0, float(self.minimum_distance.get().replace(",", ".")))
        except ValueError:
            raise ValueError("Minimalny dystans musi być liczbą.")

    def analyze_files(self) -> None:
        source = Path(self.report_path.get())
        if not source.is_file():
            messagebox.showwarning(
                "Brak pliku",
                "Wybierz poprawny raport XLSX.",
            )
            return
        try:
            minimum_distance = self._minimum_distance_value()
        except ValueError as exc:
            messagebox.showwarning("Niepoprawna wartość", str(exc))
            return

        self._set_loading(True)
        self.status_text.set("Wczytuję raport i zapisuję go w archiwum...")

        def worker() -> None:
            archive_path = None
            archive_remote_id = None
            archive_error = None
            try:
                records = load_single_report(source)
                result = analyze_records(
                    records,
                    driver_overrides=self.driver_overrides,
                    minimum_distance=minimum_distance,
                )
                if self.db_config.is_complete:
                    try:
                        archive_remote_id = save_database_archive(self.db_config, records, source.name)
                    except Exception as exc:
                        archive_error = exc
                        archive_path = self._archive_path_for(source)
                        try:
                            save_archive_report(archive_path, records, source.name)
                        except OSError as local_exc:
                            archive_path = None
                            archive_error = local_exc
                else:
                    archive_path = self._archive_path_for(source)
                    try:
                        save_archive_report(archive_path, records, source.name)
                    except OSError as exc:
                        archive_path = None
                        archive_error = exc
            except Exception as exc:
                self.after(0, lambda error=exc: self._analysis_failed(error))
                return
            self.after(
                0,
                lambda: self._analysis_ready(
                    records,
                    result,
                    source_label=source.name,
                    archive_path=archive_path,
                    archive_remote_id=archive_remote_id,
                    archive_error=archive_error,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _analysis_ready(
        self,
        records,
        result: AnalysisResult,
        source_label: str = "",
        archive_path: Path | None = None,
        archive_remote_id: int | None = None,
        archive_error: Exception | None = None,
        from_archive: bool = False,
    ) -> None:
        self.records = records
        self.result = result
        self._set_loading(False)
        self._refresh_dashboard()
        self.export_xlsx_button.configure(state="normal")
        self.export_csv_button.configure(state="normal")
        if (archive_path or archive_remote_id) and not from_archive:
            self._refresh_archive_list()

        period = self._period_label(result.date_from, result.date_to)
        prefix = "Wczytano archiwum" if from_archive else "Wczytano raport"
        archive_note = ""
        if not from_archive:
            if archive_remote_id:
                archive_note = " Raport zapisano w bazie PostgreSQL."
            elif archive_path:
                archive_note = " Raport zapisano w lokalnym archiwum."
        if archive_error:
            if archive_path:
                archive_note = f" PostgreSQL niedostępny, zapisano lokalnie: {archive_error}"
            else:
                archive_note = f" Nie udało się zapisać archiwum: {archive_error}"
        source_note = f" ({source_label})" if source_label else ""
        self.status_text.set(
            f"{prefix}{source_note}: {len(records)} transakcji. "
            f"W rankingu: {result.ranked_count}. "
            f"Okres: {period}.{archive_note}"
        )

    def _analysis_failed(self, error: Exception) -> None:
        self._set_loading(False)
        self.status_text.set("Analiza nie powiodła się.")
        messagebox.showerror("Błąd analizy", str(error))

    def _set_loading(self, loading: bool) -> None:
        if loading:
            self.analyze_button.configure(state="disabled")
            if hasattr(self, "load_archive_button"):
                self.load_archive_button.configure(state="disabled")
                self.archive_combo.configure(state="disabled")
            self.progress.pack(
                fill="x", pady=(10, 0), before=self.kpi_people.master
            )
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.analyze_button.configure(state="normal")
            if hasattr(self, "load_archive_button"):
                has_archive = bool(self.archive_by_label)
                self.load_archive_button.configure(state="normal" if has_archive else "disabled")
                self.archive_combo.configure(state="readonly" if has_archive else "disabled")

    def _refresh_dashboard(self) -> None:
        if not self.result:
            return
        result = self.result
        self.kpi_people.configure(text=str(len(result.rows)))
        self.kpi_fuel.configure(text=f"{format_number(result.total_fuel)} l")
        self.kpi_distance.configure(text=f"{format_number(result.total_distance, 1)} km")
        self.kpi_consumption.configure(
            text=(
                f"{format_number(result.average_consumption)} l/100 km"
                if result.average_consumption is not None
                else "-"
            )
        )
        if result.worst and result.worst.consumption is not None:
            self.kpi_worst.configure(
                text=f"{result.worst.driver} | {format_number(result.worst.consumption)} l/100 km"
            )
        else:
            self.kpi_worst.configure(text="Brak danych do rankingu")

        warning = "  ".join(result.warnings)
        self.warning_text.set(warning)
        if warning:
            self.warning_bar.pack(fill="x", pady=(10, 0), before=self.kpi_people.master)
        else:
            self.warning_bar.pack_forget()
        self._fill_table()

    def _rows_for_display(self) -> list[DriverSummary]:
        if not self.result:
            return []
        rows = list(self.result.rows)
        query = self.search_text.get().strip().casefold()
        if query:
            rows = [
                row
                for row in rows
                if query in row.driver.casefold()
                or query in row.vehicle_label.casefold()
                or query in row.status.casefold()
            ]

        def sort_value(row: DriverSummary):
            values = {
                "rank": row.rank if row.rank is not None else 10**9,
                "driver": row.driver.casefold(),
                "vehicles": row.vehicle_label.casefold(),
                "transactions": row.transactions,
                "diesel_total": row.diesel_total,
                "gasoline": row.gasoline_liters,
                "fuel_total": row.fuel_total,
                "adblue": row.adblue_liters,
                "distance": row.distance_km,
                "consumption": row.consumption if row.consumption is not None else -1,
                "eur": row.cost_eur,
                "pln": row.cost_pln,
                "status": row.status.casefold(),
            }
            return values[self.sort_column]

        return sorted(rows, key=sort_value, reverse=self.sort_reverse)

    def _fill_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.iid_to_row.clear()
        for index, row in enumerate(self._rows_for_display()):
            iid = f"result-{index}"
            self.iid_to_row[iid] = row
            tags = []
            if row.rank == 1:
                tags.append("worst")
            elif row.rank is None:
                tags.append("unranked")
            elif index % 2:
                tags.append("odd")
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    row.rank or "-",
                    row.driver,
                    row.vehicle_label,
                    row.transactions,
                    format_number(row.diesel_total),
                    format_number(row.gasoline_liters),
                    format_number(row.fuel_total),
                    format_number(row.adblue_liters),
                    format_number(row.distance_km, 1),
                    format_number(row.consumption),
                    format_number(row.cost_eur),
                    format_number(row.cost_pln),
                    row.status,
                ),
                tags=tuple(tags),
            )

    def sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = column not in {"rank", "driver", "vehicles", "status"}
        self._fill_table()

    def _selection_changed(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            self.assign_button.configure(state="disabled")
            return
        row = self.iid_to_row.get(selected[0])
        if not row:
            return
        self.assign_button.configure(state="normal")
        self.driver_name.set(
            "" if row.driver.startswith("Nieprzypisany -") else row.driver
        )

    def assign_driver(self) -> None:
        selected = self.tree.selection()
        if not selected or not self.result:
            return
        row = self.iid_to_row.get(selected[0])
        if not row:
            return
        name = self.driver_name.get().strip()
        for vehicle in row.vehicles:
            if name:
                self.driver_overrides[vehicle] = name
            else:
                self.driver_overrides.pop(vehicle, None)
        try:
            save_driver_mapping(MAPPING_PATH, self.driver_overrides)
        except OSError as exc:
            messagebox.showwarning(
                "Nie zapisano mapowania",
                f"Wynik został przeliczony, ale nie udało się zapisać mapowania:\n{exc}",
            )
        self.result = analyze_records(
            self.records,
            driver_overrides=self.driver_overrides,
            minimum_distance=self._minimum_distance_value(),
        )
        self._refresh_dashboard()
        self.status_text.set(
            f"Przypisano kierowcę „{name or 'brak'}” do: {', '.join(row.vehicles)}."
        )

    def export_to_xlsx(self) -> None:
        if not self.result:
            return
        selected = filedialog.asksaveasfilename(
            title="Zapisz raport XLSX",
            initialdir=BASE_DIR,
            initialfile="raport_kierowcow.xlsx",
            defaultextension=".xlsx",
            filetypes=[("Plik Excel", "*.xlsx")],
        )
        if not selected:
            return
        try:
            export_xlsx(self.result, selected)
        except Exception as exc:
            messagebox.showerror("Błąd eksportu", str(exc))
            return
        self.status_text.set(f"Zapisano raport XLSX: {selected}")
        messagebox.showinfo("Raport zapisany", "Raport XLSX został zapisany poprawnie.")

    def export_to_csv(self) -> None:
        if not self.result:
            return
        selected = filedialog.asksaveasfilename(
            title="Zapisz raport CSV",
            initialdir=BASE_DIR,
            initialfile="raport_kierowcow.csv",
            defaultextension=".csv",
            filetypes=[("Plik CSV", "*.csv")],
        )
        if not selected:
            return
        try:
            export_csv(self.result, selected)
        except Exception as exc:
            messagebox.showerror("Błąd eksportu", str(exc))
            return
        self.status_text.set(f"Zapisano raport CSV: {selected}")
        messagebox.showinfo("Raport zapisany", "Raport CSV został zapisany poprawnie.")


if __name__ == "__main__":
    configure_logging("fuel_insight.gui")
    app = FuelInsightApp()
    app.mainloop()
