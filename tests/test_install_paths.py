"""路径契约回归；配置、数据库和日志写入均隔离到临时目录。"""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from utils import paths


class TestDataRoot(unittest.TestCase):
    def test_source_default_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop("DPD_DATA_ROOT", None)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            result = subprocess.run(
                [sys.executable, "-c", "from utils.paths import DATA_ROOT; print(DATA_ROOT)"],
                cwd=tmp, env=env, capture_output=True, text=True, check=True,
            )
            self.assertEqual(Path(result.stdout.strip()), Path(__file__).resolve().parents[1])

    def test_explicit_root_precedes_source_and_xdg(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DPD_DATA_ROOT="chosen", XDG_DATA_HOME="/ignored")
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            result = subprocess.run(
                [sys.executable, "-c", "from utils.paths import DATA_ROOT; print(DATA_ROOT)"],
                cwd=tmp, env=env, capture_output=True, text=True, check=True,
            )
            self.assertEqual(Path(result.stdout.strip()), Path(tmp) / "chosen")

    def test_user_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {}, clear=True):
                with patch.object(sys, "platform", "linux"):
                    self.assertEqual(paths._user_data_root(), home / ".local/share/daily-paper-digest")
                    with patch.dict(os.environ, {"XDG_DATA_HOME": str(home / "xdg")}):
                        self.assertEqual(paths._user_data_root(), home / "xdg/daily-paper-digest")
                    with patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}):
                        self.assertEqual(paths._user_data_root(), home / ".local/share/daily-paper-digest")
                with patch.object(sys, "platform", "darwin"):
                    self.assertEqual(paths._user_data_root(), home / "Library/Application Support/daily-paper-digest")
                with patch.object(sys, "platform", "win32"):
                    self.assertEqual(paths._user_data_root(), home / "AppData/Local/daily-paper-digest")
                    with patch.dict(os.environ, {"LOCALAPPDATA": str(home / "local")}):
                        self.assertEqual(paths._user_data_root(), home / "local/daily-paper-digest")

    def test_template_initialization_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(paths, "DATA_ROOT", Path(tmp)):
            destination = paths.init_config()
            self.assertEqual(destination, Path(tmp) / "config/config.yaml")
            self.assertIn("database:", destination.read_text())
            destination.write_text("my private config", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                paths.init_config()
            self.assertEqual(destination.read_text(), "my private config")

    def test_config_loading_database_lock_output_and_backup(self):
        import main
        from core.db import Database
        from core.notifier import Notifier
        import sqlite3
        import yaml

        with tempfile.TemporaryDirectory() as tmp, patch.object(paths, "DATA_ROOT", Path(tmp)):
            cfg = {
                "database": {"path": "data/db/test.db"},
                "output": {"output_dir": "data/output"},
                "fetcher": {"upload_dir": "data/uploads"},
                "llm": {"provider": "fake", "fake": {"api_key": "fake"}},
            }
            original = copy.deepcopy(cfg)
            destination = paths.init_config()
            destination.write_text(yaml.safe_dump(cfg), encoding="utf-8")
            in_memory = main.load_config_from_obj(cfg)
            self.assertFalse((Path(tmp) / "data/db").exists())
            loaded = main.load_config()
            self.assertTrue((Path(tmp) / "data/db").is_dir())
            self.assertEqual(cfg, original)
            self.assertEqual(loaded, in_memory)
            self.assertEqual(loaded["database"]["path"], str(Path(tmp) / "data/db/test.db"))
            self.assertEqual(loaded["fetcher"]["upload_dir"], str(Path(tmp) / "data/uploads"))
            lock = main.RunLock(cfg)
            self.assertEqual(lock._lock_path, Path(tmp) / "data/db/.pipeline.lock")
            lock.acquire()
            lock.release()
            db = Database(loaded["database"]["path"])
            try:
                self.assertEqual(db.db_path, loaded["database"]["path"])
            finally:
                db.close()
            self.assertEqual(Notifier(loaded).output_dir, Path(tmp) / "data/output")
            backup = Path(main.backup_database(cfg))
            self.assertEqual(backup.parent, Path(tmp) / "data/backups")
            with sqlite3.connect(backup) as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            cfg["backup"] = {"directory": "saved"}
            self.assertEqual(Path(main.backup_database(cfg)).parent, Path(tmp) / "saved")
            absolute = str(Path(tmp) / "elsewhere.db")
            self.assertEqual(main.load_config_from_obj({"database": {"path": absolute}})["database"]["path"], absolute)
            self.assertEqual(main.load_config_from_obj({"database": {"path": ":memory:"}})["database"]["path"], ":memory:")

    def test_backup_includes_committed_wal_pages(self):
        import sqlite3
        import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = sqlite3.connect(root / "live.db")
            self.addCleanup(source.close)
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("CREATE TABLE proof(value TEXT)")
            source.execute("INSERT INTO proof VALUES ('committed WAL content')")
            source.commit()
            config = {"database": {"path": str(root / "live.db")},
                      "backup": {"directory": str(root / "backups")}}
            path = main.backup_database(config)
            backup = sqlite3.connect(path)
            try:
                self.assertEqual(backup.execute("SELECT value FROM proof").fetchone()[0],
                                 "committed WAL content")
            finally:
                backup.close()

    def test_cli_init_writes_only_to_explicit_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "chosen"
            env = dict(os.environ, DPD_DATA_ROOT=str(root), PYTHONDONTWRITEBYTECODE="1")
            # 继承测试所用的依赖路径，同时确保源码可从任意 cwd 导入。
            env["PYTHONPATH"] = os.pathsep.join(sys.path)
            command = [sys.executable, "-m", "main", "--init-config"]
            result = subprocess.run(command, cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            config = root / "config/config.yaml"
            before = config.read_bytes()
            self.assertTrue(list((root / "data/logs").glob("*.log")))
            self.assertFalse((root / "data/db").exists())
            again = subprocess.run(command, cwd=tmp, env=env, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((Path(tmp) / "config").exists())


if __name__ == "__main__":
    unittest.main()
