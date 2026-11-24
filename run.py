#!/usr/bin/env python
"""项目根目录启动器：把仓库根加入路径后以隔离模式运行履约数据仓 CLI。

使用 ``-I`` 是为了读取不受信任的交接目录时，避免其中植入的同名模块被导入；
本脚本自身只负责定位项目包，业务代码全部在 ``fulfillment`` 包内。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fulfillment.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
