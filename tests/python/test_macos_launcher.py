import os
import pty
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "macos" / "launcher.sh"
APPLESCRIPT = ROOT / "macos" / "AppLauncher.applescript"


class LauncherTests(unittest.TestCase):
    def test_launcher_has_no_dynamic_code_or_path_search(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        for forbidden in ("source ", "eval ", "/usr/bin/env python", "command -v", "which ", "dirname", "grep"):
            self.assertNotIn(forbidden, text)
        self.assertIn('IFS= read -r PYTHON_EXECUTABLE', text)
        self.assertIn('"$PYTHON_EXECUTABLE" -B "$RESOURCES/runtime.py"', text)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", text)

    def test_real_python_launch_does_not_write_bytecode_into_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            resources = Path(directory) / "Resources"
            manager = resources / "manager"
            manager.mkdir(parents=True)
            launcher = resources / "launcher.sh"
            launcher.write_bytes(LAUNCHER.read_bytes())
            (resources / "python-path").write_text(f"{Path(sys.executable).resolve()}\n", encoding="utf-8")
            (manager / "probe.py").write_text("VALUE = 1\n", encoding="utf-8")
            (resources / "runtime.py").write_text("from manager import probe\nassert probe.VALUE == 1\n", encoding="utf-8")

            hostile_env = os.environ.copy()
            hostile_env["PYTHONDONTWRITEBYTECODE"] = "0"
            result = subprocess.run(
                ["/bin/bash", str(launcher)],
                env=hostile_env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], list(resources.rglob("__pycache__")))
            self.assertEqual([], list(resources.rglob("*.pyc")))

    def test_python_path_rejects_relative_and_multiple_lines(self):
        for content in ("relative/python\n", "/bin/true\n/bin/false\n", "/bin/true\n/bin/false"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                resources = Path(directory)
                launcher = resources / "launcher.sh"
                launcher.write_bytes(LAUNCHER.read_bytes())
                (resources / "python-path").write_text(content, encoding="utf-8")
                (resources / "runtime.py").write_text("", encoding="utf-8")
                result = subprocess.run(
                    ["bash", str(launcher)],
                    input="\n", text=True, capture_output=True, check=False,
                )
                self.assertNotEqual(0, result.returncode)

    def test_hostile_path_cannot_run_a_program_during_launcher_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resources = root / "Resources"
            hostile = root / "hostile"
            resources.mkdir()
            hostile.mkdir()
            launcher = resources / "launcher.sh"
            launcher.write_bytes(LAUNCHER.read_bytes())
            (resources / "python-path").write_text("/usr/bin/true\n", encoding="utf-8")
            (resources / "runtime.py").write_text("", encoding="utf-8")
            marker = root / "executed"
            for name in ("dirname", "grep"):
                program = hostile / name
                program.write_text(f"#!/bin/sh\n: > '{marker}'\nexit 1\n", encoding="utf-8")
                program.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(launcher)],
                env={"PATH": str(hostile)}, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(marker.exists())

    def test_launcher_preserves_status_messages_in_japanese_and_waits_only_on_tty(self):
        for status, expected in ((0, "処理が正常に完了しました。"), (23, "処理に失敗しました（終了コード: 23）。")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                resources = Path(directory)
                launcher = resources / "launcher.sh"
                launcher.write_bytes(LAUNCHER.read_bytes())
                runtime = resources / "python"
                runtime.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
                runtime.chmod(0o755)
                (resources / "python-path").write_text(f"{runtime}\n", encoding="utf-8")
                (resources / "runtime.py").write_text("", encoding="utf-8")
                non_tty = subprocess.run(
                    ["/bin/bash", str(launcher)], input="", text=True,
                    capture_output=True, check=False,
                )
                self.assertEqual(status, non_tty.returncode)
                self.assertIn(expected, non_tty.stdout)
                self.assertNotIn("Enterキー", non_tty.stdout)

                master, slave = pty.openpty()
                try:
                    process = subprocess.Popen(
                        ["/bin/bash", str(launcher)], stdin=slave, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                    )
                    os.close(slave)
                    os.write(master, b"\n")
                    stdout, stderr = process.communicate(timeout=5)
                finally:
                    os.close(master)
                self.assertEqual(status, process.returncode, stderr)
                self.assertIn(expected, stdout)
                self.assertIn("Enterキーを押して閉じてください。", stdout)

    def test_applescript_only_launches_quoted_fixed_resource(self):
        text = APPLESCRIPT.read_text(encoding="utf-8")
        self.assertIn("path to me", text)
        self.assertIn('"/Contents/Resources/launcher.sh"', text)
        self.assertIn("«event coredosc» (quoted form of launcherPath)", text)
        self.assertNotIn("«event coredoex»", text)
        for forbidden in ("path to resource", "python", "XSERVER_", "config", "environment", " with administrator privileges", "do shell script", ";", "&&", "||"):
            self.assertNotIn(forbidden, text)

    def test_applescript_compiles_as_an_application(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["/usr/bin/osacompile", "-o", str(Path(directory) / "Launcher.app"), str(APPLESCRIPT)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
