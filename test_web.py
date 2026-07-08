import unittest

from fuel_insight_web import _extract_upload, _safe_filename


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


if __name__ == "__main__":
    unittest.main()
