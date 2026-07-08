from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook

from archive_database import DatabaseConfig
from fuel_insight_service import ServiceConfig, _database_ready_for_watch, run_once


class FuelInsightServiceTests(unittest.TestCase):
    def test_watch_database_check_does_not_exit_when_required_db_is_missing(self):
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            config = ServiceConfig(
                input_dir=base / "inbox",
                output_dir=base / "outbox",
                processed_dir=base / "processed",
                failed_dir=base / "failed",
                archive_dir=base / "archive",
                mapping_path=base / "kierowcy.json",
                db_config_path=base / "missing-database.json",
                db_config=DatabaseConfig(),
                minimum_distance=1,
                poll_interval=5,
                require_db=True,
                export_csv=True,
            )

            self.assertFalse(_database_ready_for_watch(config))

    def test_processes_single_report_without_database(self):
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "inbox" / "report.xlsx"
            source.parent.mkdir()

            workbook = Workbook()
            sheet = workbook.active
            sheet.append(
                [
                    "Numery rejestracyjne",
                    "Data",
                    "Ilość",
                    "Rodzaj towaru",
                    "Kilometry (telematyka)",
                ]
            )
            sheet.append(["ABC 123", "2026-06-01 08:00:00", 10, "Benzyna", 1000])
            sheet.append(["ABC 123", "2026-06-02 08:00:00", 20, "Diesel", 1150])
            workbook.save(source)

            config = ServiceConfig(
                input_dir=base / "inbox",
                output_dir=base / "outbox",
                processed_dir=base / "processed",
                failed_dir=base / "failed",
                archive_dir=base / "archive",
                mapping_path=base / "kierowcy.json",
                db_config_path=base / "database.json",
                db_config=DatabaseConfig(),
                minimum_distance=1,
                poll_interval=5,
                require_db=False,
                export_csv=True,
            )

            failures = run_once(config, source)

            self.assertEqual(failures, 0)
            self.assertFalse(source.exists())
            self.assertEqual(len(list((base / "processed").glob("*.xlsx"))), 1)
            self.assertEqual(len(list((base / "archive").glob("*.json"))), 1)
            self.assertEqual(len(list((base / "outbox").glob("*_raport.xlsx"))), 1)
            self.assertEqual(len(list((base / "outbox").glob("*_raport.csv"))), 1)
            self.assertEqual(list((base / "failed").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
