import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AgyHarvestTests(unittest.TestCase):
    @staticmethod
    def _transcript(home: Path, root: str, session: str) -> Path:
        path = home / ".gemini" / root / "brain" / session / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True)
        return path

    @staticmethod
    def _agy_line(content: str) -> str:
        return json.dumps({
            "type": "PLANNER_RESPONSE",
            "content": content,
            "created_at": "2026-08-22T07:20:04Z",
            "source": "MODEL",
            "status": "DONE",
            "step_index": 1,
        }) + "\n"

    def test_harvests_both_roots_idempotently_and_prefers_current_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home = tmp_path / "home"
            logs = tmp_path / "logs"
            current = self._transcript(home, "antigravity-cli", "shared-session")
            duplicate = self._transcript(home, "antigravity", "shared-session")
            legacy = self._transcript(home, "antigravity", "legacy-session")
            current.write_text(self._agy_line("current source"))
            duplicate.write_text(self._agy_line("legacy duplicate"))
            legacy.write_text(self._agy_line("legacy source"))
            for source in (current, duplicate, legacy):
                os.utime(source, (1, 1))

            env = os.environ.copy()
            env["HOME"] = str(home)
            command = ["make", "harvest", f"LOGS={logs}"]
            subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=True)

            shared_output = logs / "agy" / "default" / "shared-session.jsonl"
            legacy_output = logs / "agy" / "default" / "legacy-session.jsonl"
            self.assertIn("current source", shared_output.read_text())
            self.assertNotIn("legacy duplicate", shared_output.read_text())
            self.assertIn("legacy source", legacy_output.read_text())
            self.assertTrue(shared_output.with_suffix(".md").exists())

            first_mtime = shared_output.stat().st_mtime_ns
            subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
            self.assertEqual(shared_output.stat().st_mtime_ns, first_mtime)

            current.write_text(self._agy_line("updated current source"))
            newer = max(time.time_ns(), first_mtime + 2_000_000_000)
            os.utime(current, ns=(newer, newer))
            subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=True)
            self.assertIn("updated current source", shared_output.read_text())


class InstallCronTests(unittest.TestCase):
    def test_install_cron_captures_path_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cron_state = tmp_path / "cron-state"
            fake_crontab = tmp_path / "crontab-bin"
            fake_codex = tmp_path / "codex"

            fake_crontab.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-l\" ]; then\n"
                "  [ ! -f \"$CRON_STATE\" ] || cat \"$CRON_STATE\"\n"
                "else\n"
                "  cat > \"$CRON_STATE\"\n"
                "  [ \"$CRON_FAIL\" != 1 ]\n"
                "fi\n"
            )
            fake_codex.write_text("#!/bin/sh\nexit 0\n")
            fake_crontab.chmod(0o755)
            fake_codex.chmod(0o755)

            runtime_path = f"{tmp_path}:/usr/bin:/bin"
            env = os.environ.copy()
            env.update({
                "PATH": runtime_path,
                "CRON_STATE": str(cron_state),
            })

            # The Makefile calls `crontab`; expose the fake under that name while
            # retaining a separate state file for assertions.
            (tmp_path / "crontab").symlink_to(fake_crontab)

            for _ in range(2):
                result = subprocess.run(
                    ["make", "install-cron"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )

            installed = cron_state.read_text()
            self.assertEqual(installed.count("# ai-distillery-cron"), 1)
            expected_path = f"{tmp_path}:/usr/local/bin:/usr/bin:/bin"
            self.assertIn(f"export PATH={expected_path};", installed)
            self.assertIn(str(fake_codex), result.stdout)
            self.assertIn("47 8 * * *", installed)
            self.assertIn("make leader", installed)
            self.assertNotIn("/data/home/", installed)

            subprocess.run(
                ["make", "install-cron", "CRON_ROLE=collector"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            installed = cron_state.read_text()
            self.assertEqual(installed.count("# ai-distillery-cron"), 1)
            self.assertIn("47 7 * * *", installed)
            self.assertIn("make collector", installed)

            invalid = subprocess.run(
                ["make", "install-cron", "CRON_ROLE=both"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("CRON_ROLE must be leader or collector", invalid.stderr)

            env["CRON_FAIL"] = "1"
            failed = subprocess.run(
                ["make", "install-cron"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("ERROR: failed to install cron", failed.stderr)


if __name__ == "__main__":
    unittest.main()
