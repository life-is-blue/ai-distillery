import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
            self.assertIn(f"export PATH={runtime_path};", installed)
            self.assertIn(str(fake_codex), result.stdout)
            self.assertNotIn("/data/home/", installed)


if __name__ == "__main__":
    unittest.main()
