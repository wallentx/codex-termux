#!/usr/bin/env python3
"""Exercise the real Android archive and installer without building Rust."""

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


builder = load("package_builder", ROOT / ".github/scripts/termux-package.py")
installer = load("package_installer", ROOT / "scripts/get-codex")


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.host = self.root / "codex-code-mode-host"
        for path, text in ((self.codex, "cli payload"), (self.host, "host payload")):
            path.write_text(text)
            path.chmod(0o755)
        self.archive = self.root / "codex-aarch64-linux-android.tar.gz"
        builder.build_package(self.codex, self.host, "1.2.3-alpha.4", self.archive)

    def test_package_layout_and_system_rg(self):
        with tarfile.open(self.archive) as archive:
            files = {
                member.name.removeprefix("./") for member in archive if member.isfile()
            }
            self.assertEqual(
                files, {"bin/codex", "bin/codex-code-mode-host", "codex-package.json"}
            )
            metadata = json.load(archive.extractfile("./codex-package.json"))
        self.assertEqual(
            metadata,
            {
                "layoutVersion": 1,
                "version": "1.2.3-alpha.4",
                "target": "aarch64-linux-android",
                "variant": "codex",
                "entrypoint": "bin/codex",
                "resourcesDir": "codex-resources",
                "pathDir": "codex-path",
            },
        )

    def test_install_actions_zip_preserves_bundle_and_previous_launcher(self):
        artifact = self.root / "artifact.zip"
        with zipfile.ZipFile(artifact, "w") as zip_file:
            zip_file.write(self.archive, f"nested/{self.archive.name}")
            # An unrelated compressed helper must not be mistaken for Codex.
            zip_file.writestr("codex-code-mode-host.zst", "wrong payload")
        target = self.root / "bin/codex"
        target.parent.mkdir()
        target.write_text("previous cli")
        downloads = self.root / "downloads"
        installer.install(artifact, downloads, target)
        self.assertTrue(target.is_symlink())
        package = target.resolve().parent.parent
        self.assertEqual(target.read_text(), "cli payload")
        self.assertEqual(
            (package / "bin/codex-code-mode-host").read_text(), "host payload"
        )
        self.assertTrue((package / "codex-package.json").is_file())
        self.assertTrue((package / "codex-path").is_dir())
        self.assertFalse((package / "codex-path/rg").exists())
        self.assertEqual(
            [p.read_text() for p in downloads.glob("previous-codex-*")],
            ["previous cli"],
        )

    def test_incomplete_bundle_does_not_replace_existing_install(self):
        incomplete = self.root / "incomplete.tar.gz"
        with (
            tarfile.open(self.archive) as source,
            tarfile.open(incomplete, "w:gz") as output,
        ):
            for member in source:
                if member.name.endswith("codex-code-mode-host"):
                    continue
                output.addfile(
                    member, source.extractfile(member) if member.isfile() else None
                )
        target = self.root / "installed"
        target.write_text("previous cli")
        with self.assertRaisesRegex(ValueError, "Missing executable"):
            installer.install(incomplete, self.root / "downloads", target)
        self.assertEqual(target.read_text(), "previous cli")

    def test_cli_reads_the_actual_cargo_version(self):
        manifest = self.root / "Cargo.toml"
        manifest.write_text('[workspace.package]\nversion = "2.0.0-dev"\n')
        output = self.root / "dev.tar.gz"
        subprocess.run(
            [
                "python3",
                "-B",
                str(ROOT / ".github/scripts/termux-package.py"),
                "--codex",
                str(self.codex),
                "--code-mode-host",
                str(self.host),
                "--cargo-manifest",
                str(manifest),
                "--output",
                str(output),
            ],
            check=True,
        )
        with tarfile.open(output) as archive:
            self.assertEqual(
                json.load(archive.extractfile("./codex-package.json"))["version"],
                "2.0.0-dev",
            )

    def test_flat_legacy_archive_still_installs_both_binaries(self):
        legacy = self.root / "legacy.tar.gz"
        with tarfile.open(legacy, "w:gz") as archive:
            archive.add(self.codex, arcname="codex")
            archive.add(self.host, arcname="codex-code-mode-host")
        target = self.root / "bin/codex"
        installer.install(legacy, self.root / "downloads", target)
        self.assertEqual(target.read_text(), "cli payload")
        self.assertEqual(
            target.with_name("codex-code-mode-host").read_text(), "host payload"
        )


if __name__ == "__main__":
    unittest.main()
