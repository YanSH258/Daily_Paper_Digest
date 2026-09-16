import unittest
from unittest.mock import patch

import web_server


class P0ModeTests(unittest.TestCase):
    def test_task_runner_passes_trial_and_preview_flags(self):
        runner = web_server.TaskRunner({"database": {"path": "/tmp/p0-test.db"}})
        runner.attach_db(None)  # type: ignore[arg-type]
        with patch("web_server.run_once", return_value={}) as run_once, \
             patch.object(web_server.threading.Thread, "start"):
            ok, _ = runner.start_run("manual", "light", run_mode="trial")
            self.assertTrue(ok)
        # The worker is intentionally exercised directly to avoid a race with Thread.start.
        with patch("web_server.run_once", return_value={}) as run_once:
            runner._run_task("t", "manual", "light", None, "preview", False)
            run_once.assert_called_once_with(
                {"database": {"path": "/tmp/p0-test.db"}, "fetcher": {"use_fulltext": False}, "analyzer": {"analyze_abstract_only": False}},
                date_str=None, task_id="t", trial=False, preview=True, refresh=False
            )

    def test_invalid_run_mode_is_rejected(self):
        runner = web_server.TaskRunner({"database": {"path": "/tmp/p0-test.db"}})
        runner.attach_db(None)  # type: ignore[arg-type]
        ok, message = runner.start_run("manual", run_mode="bad")
        self.assertFalse(ok)
        self.assertIn("normal/trial/preview", message)


if __name__ == "__main__":
    unittest.main()
