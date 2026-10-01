#!/usr/bin/env python3
"""Reconcile only file-lock edges in Cargo.lock, without resolving dependencies."""

import re
import subprocess
import sys
import tomllib
from pathlib import Path


def sync_file_lock(workspace: Path) -> None:
    dependency = "codex-utils-file-lock"
    manifests = subprocess.check_output(
        ["git", "ls-files", "-z", "--", f"{workspace}/**/Cargo.toml"], text=True
    ).split("\0")
    consumers = set()
    for manifest in filter(None, manifests):
        data = tomllib.loads(Path(manifest).read_text())
        tables = [data, *data.get("target", {}).values()]
        if any(
            dependency in table.get(kind, {})
            for table in tables
            for kind in ("dependencies", "dev-dependencies", "build-dependencies")
        ):
            consumers.add(data["package"]["name"])

    lockfile = workspace / "Cargo.lock"
    original = lockfile.read_text()
    packages = tomllib.loads(original)["package"]
    if missing := consumers - {package["name"] for package in packages}:
        raise ValueError(f"file-lock consumers missing from Cargo.lock: {sorted(missing)}")
    if consumers and not any(package["name"] == dependency for package in packages):
        raise ValueError("file-lock package missing from Cargo.lock")

    def reconcile(match: re.Match[str]) -> str:
        block = match.group()
        package = tomllib.loads(block)["package"][0]
        wanted = package["name"] in consumers
        present = dependency in package.get("dependencies", [])
        if wanted == present:
            return block
        entry = f' "{dependency}",\n'
        if not wanted:
            if entry not in block:
                raise ValueError("unexpected file-lock dependency format")
            return block.replace(entry, "")
        dependencies = re.search(r"^dependencies = \[\n(.*?)^\]", block, re.M | re.S)
        if dependencies is None:
            raise ValueError(f"dependency list missing for {package['name']}")
        lines = dependencies[1].splitlines(keepends=True)
        index = next((i for i, line in enumerate(lines) if line > entry), len(lines))
        lines.insert(index, entry)
        return (
            block[: dependencies.start(1)]
            + "".join(lines)
            + block[dependencies.end(1) :]
        )

    updated = re.sub(
        r"^\[\[package\]\]\n.*?(?=^\[\[package\]\]|\Z)",
        reconcile,
        original,
        flags=re.M | re.S,
    )
    result = tomllib.loads(updated)
    actual = {
        package["name"]
        for package in result["package"]
        if dependency in package.get("dependencies", [])
    }
    if actual != consumers:
        raise ValueError("file-lock dependency reconciliation failed")
    if updated != original:
        lockfile.write_text(updated)


if __name__ == "__main__":
    sync_file_lock(Path(sys.argv[1] if len(sys.argv) > 1 else "codex-rs"))
