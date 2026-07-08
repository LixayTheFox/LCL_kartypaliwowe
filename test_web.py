from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook

from archive_database import DatabaseConfig
from fuel_insight_service import ServiceConfig
from fuel_insight_web import Upload, _compare_uploads, _extract_named_uploads, _extract_upload, _safe_filename


def _xlsx_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append([
        "Numery rejestracyjne",
        "Data",
        "Ilość",
        "Rodzaj towaru",
        "Kilometry (telematyka)",
    ])
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class FuelInsightWebTests(unittest.TestCase):
    def test_safe_filename_accepts_xlsx_and_removes_paths(self):
        self.assertEqual(_safe_filename(r"C:\fake\Raport 01.xlsx"), "Raport_01.xlsx")

    def test_safe_filename_rejects_non_xlsx(self):
        with self.assertRaises(ValueError):
            _safe_filename("report.csv")

    def test_extract_upload_reads_multipart_file(self):
        boundary = "----fuel-insight-test"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="report"; filename="report.xlsx"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n"
            "\r\n"
        ).encode("ascii") + b"sample-xlsx-bytes" + f"\r\n--{boundary}--\r\n".encode("ascii")

        upload = _extract_upload(body, f"multipart/form-data; boundary={boundary}")

        self.assertEqual(upload.filename, "report.xlsx")
        self.assertEqual(upload.content, b"sample-xlsx-bytes")

    def test_extract_named_uploads_reads_compare_files(self):
        boundary = "----fuel-insight-compare"
        parts = []
        for field, filename, content in (
            ("left_report", "left.xlsx", b"left-bytes"),
            ("right_report", "right.xlsx", b"right-bytes"),
        ):
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                    "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n"
                    "\r\n"
                ).encode("ascii") + content + b"\r\n"
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode("ascii")

        from fuel_insight_web import _extract_named_uploads

        uploads = _extract_named_uploads(
            body,
            f"multipart/form-data; boundary={boundary}",
            ("left_report", "right_report"),
        )

        self.assertEqual(uploads["left_report"].filename, "left.xlsx")
        self.assertEqual(uploads["right_report"].content, b"right-bytes")

    def test_compare_uploads_builds_two_analysis_results(self):
        with TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
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
            left = Upload(
                "left.xlsx",
                _xlsx_bytes([
                    ["ABC 123", "2026-06-01", 10, "Diesel", 1000],
                    ["ABC 123", "2026-06-02", 10, "Diesel", 1100],
                ]),
            )
            right = Upload(
                "right.xlsx",
                _xlsx_bytes([
                    ["ABC 123", "2026-06-01", 20, "Diesel", 1000],
                    ["ABC 123", "2026-06-02", 20, "Benzyna", 1200],
                ]),
            )

            comparison = _compare_uploads(left, right, config)

        self.assertEqual(comparison.left_result.total_fuel, 20)
        self.assertEqual(comparison.right_result.total_fuel, 40)
        self.assertEqual(comparison.left_result.total_distance, 100)
        self.assertEqual(comparison.right_result.total_distance, 200)


if __name__ == "__main__":
    unittest.main()