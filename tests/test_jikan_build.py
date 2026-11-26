from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name != "nt" and shutil.which("bash"), "POSIX build script")
class ManualJikanBuildTests(unittest.TestCase):
    def run_builder(self, *args: str, build_failure=False, start_failure=False):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "scripts").mkdir()
            shutil.copy2(
                root / "scripts" / "build-jikan.sh", path / "scripts" / "build-jikan.sh"
            )
            fake = path / "docker"
            fake.write_text("""#!/usr/bin/env bash
echo "$*" >> "$COMMAND_LOG"
case "$1" in
 inspect)
   if [[ "$*" == *State.Health.Status* ]]; then echo healthy; else echo sha256:old; fi;;
 run)
   if [[ "$*" == *' -r '* ]]; then
     if [[ "$*" == *:candidate* ]]; then echo v5.0.0; else echo v5.0.0; fi
   fi;;
 build) [[ "$FAIL_BUILD" != 1 ]] || exit 2;;
 compose)
   if [[ "$FAIL_START" == 1 && ! -f "$COMMAND_LOG.failed" ]]; then
     touch "$COMMAND_LOG.failed"; exit 1
   fi;;
esac
""")
            fake.chmod(0o755)
            env = {
                **os.environ,
                "PATH": str(path) + os.pathsep + os.environ["PATH"],
                "COMMAND_LOG": str(path / "log"),
                "FAIL_BUILD": str(int(build_failure)),
                "FAIL_START": str(int(start_failure)),
            }
            result = subprocess.run(
                ["bash", str(path / "scripts" / "build-jikan.sh"), *args],
                env=env,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=15,
            )
            return result, (path / "log").read_text().splitlines()

    def test_same_version_does_not_build_without_confirmation(self):
        result, commands = self.run_builder("5.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--yes", result.stdout)
        self.assertFalse(any(cmd.startswith("build ") for cmd in commands))

    def test_failed_build_leaves_running_container_intact(self):
        result, commands = self.run_builder("--yes", "5.0.0", build_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(
            any(cmd.startswith("build --pull --no-cache ") for cmd in commands)
        )
        self.assertFalse(any(cmd.startswith(("stop ", "rm ")) for cmd in commands))

    def test_successful_build_validates_then_removes_old_container(self):
        result, commands = self.run_builder("--yes", "5.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        build = next(
            i
            for i, cmd in enumerate(commands)
            if cmd.startswith("build --pull --no-cache ")
        )
        validate = next(i for i, cmd in enumerate(commands) if ":candidate -l" in cmd)
        stop = commands.index("stop anime-db-bot-Jikan")
        remove = commands.index("rm anime-db-bot-Jikan")
        start = commands.index("compose up -d --no-build --pull never jikan")
        self.assertLess(build, validate)
        self.assertLess(validate, stop)
        self.assertLess(stop, remove)
        self.assertLess(remove, start)

    def test_failed_replacement_restores_previous_image(self):
        result, commands = self.run_builder("--yes", "5.0.0", start_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tag sha256:old anime-db-bot-jikan:rollback", commands)
        self.assertIn(
            "tag anime-db-bot-jikan:rollback anime-db-bot-jikan:current", commands
        )
        self.assertEqual(
            commands.count("compose up -d --no-build --pull never jikan"), 2
        )
