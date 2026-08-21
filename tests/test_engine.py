import subprocess
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

import ai_engine


class CodexEngineTests(unittest.TestCase):
    @mock.patch.object(ai_engine.time, "sleep")
    @mock.patch.object(ai_engine.subprocess, "run")
    def test_codex_retries_once_then_succeeds(self, run, sleep):
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "", "temporary failure"),
            subprocess.CompletedProcess([], 0, "final answer\n", ""),
        ]

        with redirect_stderr(StringIO()):
            result = ai_engine._call_codex("content", "system")

        self.assertEqual(result, "final answer")
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(2)

    @mock.patch.object(ai_engine.time, "sleep")
    @mock.patch.object(ai_engine.subprocess, "run")
    def test_codex_failure_logs_actionable_stderr_tail(self, run, _sleep):
        run.return_value = subprocess.CompletedProcess(
            [], 1, "", "startup banner\n" + "x" * 2200 + "ROOT CAUSE"
        )
        stderr = StringIO()

        with redirect_stderr(stderr):
            self.assertEqual(ai_engine._call_codex("content", "system"), "")

        output = stderr.getvalue()
        self.assertIn("ROOT CAUSE", output)
        self.assertNotIn("startup banner", output)
        self.assertEqual(run.call_count, 2)

    @mock.patch.object(ai_engine.time, "sleep")
    @mock.patch.object(ai_engine.subprocess, "run")
    def test_codex_timeout_retries_once(self, run, sleep):
        run.side_effect = [
            subprocess.TimeoutExpired("codex", 300),
            subprocess.CompletedProcess([], 0, "recovered", ""),
        ]

        with redirect_stderr(StringIO()):
            result = ai_engine._call_codex("content", "system")

        self.assertEqual(result, "recovered")
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
