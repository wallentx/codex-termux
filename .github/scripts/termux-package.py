#!/usr/bin/env python3
"""Archive the Android runtime using Codex's package layout and system ripgrep."""

import argparse
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import tomllib


def build_package(codex, code_mode_host, version, output):
    for binary in (codex, code_mode_host):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(f"Missing executable: {binary}")
    if not version or any(char in version for char in "/\\\n\r"):
        raise ValueError("Invalid package version")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as scratch:
        package = Path(scratch) / "package"
        for name in ("bin", "codex-resources", "codex-path"):
            (package / name).mkdir(parents=True)
        shutil.copy2(codex, package / "bin/codex")
        shutil.copy2(code_mode_host, package / "bin/codex-code-mode-host")
        metadata = {
            "layoutVersion": 1,
            "version": version,
            "target": "aarch64-linux-android",
            "variant": "codex",
            "entrypoint": "bin/codex",
            "resourcesDir": "codex-resources",
            "pathDir": "codex-path",
        }
        (package / "codex-package.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )
        with tempfile.TemporaryDirectory(dir=output.parent) as archive_stage:
            staged = Path(archive_stage) / "package.tar.gz"
            with tarfile.open(staged, "w:gz") as archive:
                archive.add(package, arcname=".")
            os.replace(staged, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--code-mode-host", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--cargo-manifest", type=Path, default=Path("Cargo.toml"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    version = (
        args.version
        or tomllib.loads(args.cargo_manifest.read_text())["workspace"]["package"][
            "version"
        ]
    )
    build_package(args.codex, args.code_mode_host, version, args.output)


if __name__ == "__main__":
    main()
