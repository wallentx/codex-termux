#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


def resolved_v8_crate_version(repo_root: Path) -> str:
    cargo_lock_path = repo_root / "codex-rs" / "Cargo.lock"
    cargo_lock = tomllib.loads(cargo_lock_path.read_text(encoding="utf-8"))
    versions = sorted(
        {
            package["version"]
            for package in cargo_lock["package"]
            if package["name"] == "v8"
        }
    )
    if len(versions) == 1:
        return versions[0]
    if len(versions) > 1:
        raise SystemExit(f"expected exactly one resolved v8 version, found: {versions}")

    cargo_toml_path = repo_root / "codex-rs" / "Cargo.toml"
    if cargo_toml_path.exists():
        matches = sorted(
            set(
                re.findall(
                    r'^v8\s*=\s*"=?([0-9]+\.[0-9]+\.[0-9]+)"',
                    cargo_toml_path.read_text(encoding="utf-8"),
                    flags=re.MULTILINE,
                )
            )
        )
        if len(matches) == 1:
            return matches[0]

    raise SystemExit(f"expected exactly one resolved v8 version in {cargo_lock_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolved_version_parser = subparsers.add_parser("resolved-v8-crate-version")
    resolved_version_parser.add_argument("--repo-root", type=Path, required=True)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    if args.command == "resolved-v8-crate-version":
        print(resolved_v8_crate_version(repo_root))
        return 0
    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
