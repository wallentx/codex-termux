#!/usr/bin/env python3
"""Small offline tests; no Cargo invocation or external Git remote."""

import importlib.util
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "sync_file_lock", Path(__file__).with_name("termux-sync-file-lock.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FileLockSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_cwd = Path.cwd()
        os.chdir(self.temp.name)
        subprocess.run(["git", "init", "-q"], check=True)
        self.workspace = Path("codex-rs")
        for crate in ("app-server-transport", "rollout"):
            path = self.workspace / crate / "Cargo.toml"
            path.parent.mkdir(parents=True)
            path.write_text(
                f'[package]\nname = "codex-{crate}"\n\n'
                '[dependencies]\ncodex-utils-file-lock = { workspace = true }\n'
            )
        subprocess.run(["git", "add", "codex-rs"], check=True)
        self.lockfile = self.workspace / "Cargo.lock"
        self.lockfile.write_text(
            'version = 4\n\n'
            '[[package]]\nname = "codex-app-server-client"\nversion = "0.0.0"\n'
            'dependencies = [\n "codex-utils-file-lock",\n "tokio",\n]\n\n'
            '[[package]]\nname = "codex-app-server-transport"\nversion = "0.0.0"\n'
            'dependencies = [\n "codex-utils-absolute-path",\n "futures",\n]\n\n'
            '[[package]]\nname = "codex-rollout"\nversion = "0.0.0"\n'
            'dependencies = [\n "codex-utils-path",\n]\n\n'
            '[[package]]\nname = "codex-utils-file-lock"\nversion = "0.0.0"\n'
        )

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def test_corrects_misapplied_edges_without_changing_other_dependencies(self):
        before = tomllib.loads(self.lockfile.read_text())
        module.sync_file_lock(self.workspace)
        after = tomllib.loads(self.lockfile.read_text())
        expected = {
            "codex-app-server-client": ["tokio"],
            "codex-app-server-transport": [
                "codex-utils-absolute-path", "codex-utils-file-lock", "futures"
            ],
            "codex-rollout": ["codex-utils-file-lock", "codex-utils-path"],
        }
        for package in before["package"]:
            if package["name"] in expected:
                package["dependencies"] = expected[package["name"]]
        self.assertEqual(after, before)

    def test_second_run_is_byte_identical(self):
        module.sync_file_lock(self.workspace)
        before = self.lockfile.read_bytes()
        module.sync_file_lock(self.workspace)
        self.assertEqual(self.lockfile.read_bytes(), before)

    def test_missing_consumer_fails_without_writing(self):
        self.lockfile.write_text(
            self.lockfile.read_text().replace('name = "codex-rollout"', 'name = "other"')
        )
        before = self.lockfile.read_bytes()
        with self.assertRaisesRegex(ValueError, "consumers missing"):
            module.sync_file_lock(self.workspace)
        self.assertEqual(self.lockfile.read_bytes(), before)

    def test_missing_file_lock_package_fails_without_writing(self):
        self.lockfile.write_text(
            self.lockfile.read_text().replace(
                'name = "codex-utils-file-lock"', 'name = "other"'
            )
        )
        before = self.lockfile.read_bytes()
        with self.assertRaisesRegex(ValueError, "file-lock package missing"):
            module.sync_file_lock(self.workspace)
        self.assertEqual(self.lockfile.read_bytes(), before)

    def test_target_specific_dependency_is_included(self):
        manifest = self.workspace / "rollout" / "Cargo.toml"
        manifest.write_text(
            manifest.read_text().replace(
                "[dependencies]", '[target.\'cfg(target_os = "android")\'.dependencies]'
            )
        )
        module.sync_file_lock(self.workspace)
        packages = tomllib.loads(self.lockfile.read_text())["package"]
        rollout = next(p for p in packages if p["name"] == "codex-rollout")
        self.assertEqual(
            rollout["dependencies"], ["codex-utils-file-lock", "codex-utils-path"]
        )


if __name__ == "__main__":
    unittest.main()
