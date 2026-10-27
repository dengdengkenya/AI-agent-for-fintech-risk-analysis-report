"""CSV importer regression checks: python -m unittest test_import_csv_to_mysql -v."""

from contextlib import redirect_stdout
import csv
import gzip
import io
from itertools import islice
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import import_csv_to_mysql as importer


class CsvImportTests(unittest.TestCase):
    def setUp(self):
        output_root = importer.PROJECT_ROOT / "outputs"
        output_root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="csv-import-test-", dir=output_root)
        self.folder = Path(self.temporary.name).resolve()
        # All test fixture cleanup stays within the project's outputs directory.
        self.assertTrue(self.folder.is_relative_to(output_root.resolve()))
        self.addCleanup(self.temporary.cleanup)

    def write_csv(self, name, records, compressed=False):
        path = self.folder / name
        opener = gzip.open if compressed else open
        with opener(path, "wt", encoding="utf-8-sig", newline="") as source:
            csv.writer(source).writerows(records)
        return path

    def test_text_round_trip_for_plain_and_gzip(self):
        columns = ["id", "Loan Title", "Debt-To-Income Ratio", "zip_code"]
        records = [
            columns,
            ["001", 'A "quoted", multiline\r\ntitle with 中文 and U0001f600', "12.5%", "00123"],
            ["002", "", "NA", "  "],
        ]
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                path = self.write_csv("input.csv.gz" if compressed else "input.csv", records, compressed)
                self.assertEqual(importer.read_columns(path), columns)
                self.assertEqual(list(importer.iter_rows(path, columns)), [
                    tuple(records[1]), ("002", None, "NA", "  "),
                ])

    def test_only_blank_records_and_known_accepted_footers_are_skipped(self):
        columns = ["id", "desc"]
        path = self.write_csv("footers.csv", [
            columns, [], ["", ""], ["10", "loan"],
            ["Total amount funded in policy code 1: 12345", ""],
            ["Total amount funded in policy code 2: 12345"],
            ["Total amount funded in policy code 1: real text", "preserve me"],
        ])
        self.assertEqual(list(importer.iter_rows(path, columns)), [
            ("10", "loan"),
            ("Total amount funded in policy code 1: real text", "preserve me"),
        ])

    def test_malformed_record_reports_its_position(self):
        path = self.write_csv("bad.csv", [["id", "desc"], ["1", "ok"], ["2"]])
        with self.assertRaisesRegex(ValueError, "record 3 has 1 fields; expected 2"):
            list(importer.iter_rows(path, importer.read_columns(path)))

    def test_row_limit_does_not_read_beyond_requested_data(self):
        path = self.write_csv("limited.csv", [["id", "desc"], [], ["1", "ok"], ["bad"]])
        rows = importer.iter_rows(path, importer.read_columns(path))
        try:
            self.assertEqual(list(islice(rows, 1)), [("1", "ok")])
        finally:
            rows.close()

    def test_invalid_headers_fail_before_import(self):
        for columns in ([], ["ID", "id"], [""], ["bad\x00name"], ["x" * 65]):
            with self.subTest(columns=columns):
                path = self.write_csv("header.csv", [columns])
                with self.assertRaises(ValueError):
                    importer.read_columns(path)

    def test_long_description_is_preserved(self):
        value = "a" * 150_000
        path = self.write_csv("long.csv", [["id", "desc"], ["1", value]])
        self.assertEqual(list(importer.iter_rows(path, importer.read_columns(path))), [("1", value)])

    def test_quoted_identifiers(self):
        self.assertEqual(importer.quote_identifier("Loan Title"), "`Loan Title`")
        self.assertEqual(importer.quote_identifier("Debt-To-Income Ratio"), "`Debt-To-Income Ratio`")
        self.assertEqual(importer.quote_identifier("a`b"), "`a``b`")

    def test_row_and_byte_limits_preserve_every_row(self):
        rows = [(str(number), "'\\\nU0001f600") for number in range(7)]
        batches = list(importer.iter_batches(iter(rows), batch_size=3, max_bytes=100))
        self.assertEqual([row for batch in batches for row in batch], rows)
        self.assertTrue(all(len(batch) <= 3 for batch in batches))
        # A tiny packet budget must split even when the row limit is large.
        self.assertEqual(list(importer.iter_batches(iter([("a",)] * 3), 100, 10)), [
            [("a",)], [("a",)], [("a",)],
        ])

    def test_oversize_row_fails(self):
        with self.assertRaisesRegex(ValueError, "max_allowed_packet"):
            list(importer.iter_batches(iter([("large",)]), 100, 5))

    def test_gzip_fallback_and_project_relative_paths(self):
        with patch.object(importer, "PROJECT_ROOT", self.folder):
            gzip_path = self.folder / "data" / "accepted_2007_to_2018Q4.csv.gz"
            self.assertEqual(importer.default_source("accepted"), gzip_path)
            extracted = self.folder / "data" / "accepted_2007_to_2018q4.csv" / "accepted_2007_to_2018Q4.csv"
            extracted.parent.mkdir(parents=True)
            extracted.touch()
            self.assertEqual(importer.default_source("accepted"), extracted)
            self.assertEqual(importer.project_path("samples/test.csv"), self.folder / "samples/test.csv")

    def test_dry_run_validates_both_files_without_mysql(self):
        accepted = self.write_csv("accepted.csv", [["id", "desc"], ["1", "ok"]])
        rejected = self.write_csv("rejected.csv.gz", [["Loan Title"], ["loan"]], True)
        output = io.StringIO()
        with redirect_stdout(output):
            importer.main([
                "--accepted-csv", str(accepted), "--rejected-csv", str(rejected),
                "--dry-run", "--rows", "1",
            ])
        self.assertIn("Validated 1 data rows for demo.accepted.", output.getvalue())
        self.assertIn("Validated 1 data rows for demo.rejected.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
