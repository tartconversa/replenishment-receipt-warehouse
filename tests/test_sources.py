"""核对项目内的合成交接原件。"""
from pathlib import Path
import unittest

from tools.source_inventory import describe, read_records


ROOT = Path(__file__).resolve().parents[1] / "examples"


class SourceFilesTest(unittest.TestCase):
    def test_supplied_files_can_be_read(self):
        report = describe(ROOT)
        self.assertEqual(list(report), ['pick_receipts.csv', 'picks.csv', 'purchase.csv', 'receipts.csv', 'requests.csv', 'transfers.csv'])
        self.assertTrue(all(item["记录数"] and item["字段"] for item in report.values()))

    def test_supplied_related_records_are_preserved(self):
        for filename, key, value, total in [['purchase.csv', 'po_line', 'P193-1', 2], ['receipts.csv', 'receipt', 'G711', 2], ['pick_receipts.csv', 'pick', 'K31', 3]]:
            with self.subTest(filename=filename, key=key, value=value):
                records = read_records(ROOT / filename)
                self.assertEqual(sum(str(record.get(key)) == value for record in records), total)
