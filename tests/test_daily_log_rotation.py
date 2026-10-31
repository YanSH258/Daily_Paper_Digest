"""DailyFileHandler 按天滚动的回归测试。

长驻 Web 进程跨天后，日志必须写入新日期的文件（修复前 FileHandler
文件名在进程启动时固定，跨天仍写启动当天的文件）。
"""
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from main import DailyFileHandler  # noqa: E402


def _record(day: str, msg: str) -> logging.LogRecord:
    # LogRecord 构造参数不支持 created，需在构造后显式覆盖记录时间
    ts = time.mktime(time.strptime(f"{day} 10:00:00", "%Y-%m-%d %H:%M:%S"))
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
    rec.created = ts
    return rec


class DailyFileHandlerTests(unittest.TestCase):
    def test_writes_to_startup_day_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = DailyFileHandler(Path(tmp))
            handler.handle(_record("2026-09-30", "第一条"))
            handler.close()
            content = (Path(tmp) / "2026-09-30.log").read_text(encoding="utf-8")
            self.assertIn("第一条", content)

    def test_switches_to_new_file_on_day_change(self):
        """同进程跨天：第二条日志必须落到新日期文件，旧文件不被追加。"""
        with tempfile.TemporaryDirectory() as tmp:
            handler = DailyFileHandler(Path(tmp))
            try:
                handler.handle(_record("2026-09-30", "第一天的日志"))
                handler.handle(_record("2026-10-01", "第二天的日志"))
            finally:
                handler.close()
            day1 = (Path(tmp) / "2026-09-30.log").read_text(encoding="utf-8")
            day2 = (Path(tmp) / "2026-10-01.log").read_text(encoding="utf-8")
            self.assertIn("第一天的日志", day1)
            self.assertNotIn("第二天的日志", day1)
            self.assertIn("第二天的日志", day2)
            self.assertNotIn("第一天的日志", day2)

    def test_returns_to_same_file_within_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = DailyFileHandler(Path(tmp))
            try:
                handler.handle(_record("2026-09-30", "甲"))
                handler.handle(_record("2026-10-01", "乙"))
                handler.handle(_record("2026-10-01", "丙"))
            finally:
                handler.close()
            day2 = (Path(tmp) / "2026-10-01.log").read_text(encoding="utf-8")
            self.assertIn("乙", day2)
            self.assertIn("丙", day2)


if __name__ == "__main__":
    unittest.main()
