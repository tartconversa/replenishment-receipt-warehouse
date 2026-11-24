"""测试公共夹具：在临时目录构造数据库与交接文件。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fulfillment.ingest import open_warehouse, import_directory  # noqa: E402
from fulfillment.rebuild import rebuild  # noqa: E402

EXAMPLES = ROOT / "examples"


class WarehouseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "warehouse.db"
        self.conn = open_warehouse(self.db_path)
        self.import_examples()
        rebuild(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def import_examples(self) -> None:
        import_directory(self.conn, EXAMPLES)
        self.conn.commit()

    def rebuild(self) -> dict[str, int]:
        return rebuild(self.conn)
