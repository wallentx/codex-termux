#!/usr/bin/env python3
"""Transplant a downstream delta onto an exact upstream tree without checkout."""

import io
import os
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import tomllib


def git(*args, data=None, env=None):
    return subprocess.check_output(["git", *args], input=data, env=env)


def paths_between(left, right):
    return set(
        filter(
            None,
            git("diff", "--no-renames", "--name-only", "-z", left, right).split(b"\0"),
        )
    )


def validate_manifests(tree):
    paths = set(git("ls-tree", "-r", "--name-only", tree).decode().splitlines())
    manifests = sorted(
        p for p in paths if p.startswith("codex-rs/") and p.endswith("Cargo.toml")
    )
    archive = git("archive", tree, "--", *manifests)
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        data = {
            p: tomllib.loads(files.extractfile(p).read().decode()) for p in manifests
        }
    workspace = data["codex-rs/Cargo.toml"]["workspace"]
    for member in workspace["members"]:
        if f"codex-rs/{member}/Cargo.toml" not in paths:
            raise ValueError(f"missing workspace member: {member}")
    for path, manifest in data.items():
        scopes = [manifest, *manifest.get("target", {}).values()]
        if path == "codex-rs/Cargo.toml":
            scopes.append(workspace)
        for scope in scopes:
            for kind in ("dependencies", "build-dependencies", "dev-dependencies"):
                for name, spec in scope.get(kind, {}).items():
                    if not isinstance(spec, dict):
                        continue
                    if spec.get("workspace") and name not in workspace.get(
                        "dependencies", {}
                    ):
                        raise ValueError(f"{path}: missing workspace dependency {name}")
                    if "path" in spec:
                        dep = posixpath.normpath(
                            f"{posixpath.dirname(path)}/{spec['path']}/Cargo.toml"
                        )
                        if dep not in paths:
                            raise ValueError(
                                f"{path}: missing dependency manifest {dep}"
                            )


def async_updater(text):
    """Keep upstream updater arguments while carrying the async Termux caller."""
    names = r"(?:handle_app_exit|run_update_action|run_update_command)"
    text = re.sub(rf"(?m)^fn ({names})\(", r"async fn \1(", text)
    # Upstream's call sites are single-line expressions. Match the whole call,
    # including nested arguments, rather than replacing its argument list.
    return re.sub(
        rf"(?m)^([ \t]*(?:return )?{names}\([^\n]*\))(\??;?[ \t]*)$",
        r"\1.await\2",
        text,
    )


def merge_updater_additions(text):
    """Join only reviewed Termux insertions with upstream's adjacent additions."""
    additions = {
        "    /// Update by replacing the current Termux binary from wallentx/codex-termux.\n    TermuxSelfUpdate,\n",
        '            UpdateAction::TermuxSelfUpdate => ("codex", &["update"]),\n',
        "        Some(UpdateAction::TermuxSelfUpdate) => termux_update::latest_release_version().await?,\n",
        "    if matches!(action, UpdateAction::TermuxSelfUpdate) {\n        run_termux_update().await?;\n        return Ok(());\n    }\n\n",
    }
    pattern = r"^<<<<<<<[^\n]*\n(.*?)^\|\|\|\|\|\|\|[^\n]*\n(.*?)^=======\n(.*?)^>>>>>>>[^\n]*\n"

    def resolve(match):
        ours, base, theirs = match.groups()
        if not base and theirs in additions:
            return theirs + ours
        signature = (
            "async fn run_update_action(action: UpdateAction) -> anyhow::Result<()> {\n"
        )
        if (
            base == signature
            and theirs.startswith(signature)
            and theirs[len(signature) :] in additions
        ):
            current = re.match(
                r"async fn run_update_action\(\n    action: UpdateAction,\n    cli_executable: Option<&std::path::Path>,\n\) -> anyhow::Result<\(\)> \{\n",
                ours,
            )
            if current:
                return current[0] + theirs[len(signature) :] + ours[current.end() :]
        return match[0]

    return re.sub(pattern, resolve, text, flags=re.MULTILINE | re.DOTALL)


def release_tree(upstream, source, baseline, excluded):
    upstream, source, baseline = (
        git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        for ref in (upstream, source, baseline)
    )
    # A private index lets failed preflights leave the caller's checkout intact.
    with tempfile.TemporaryDirectory(
        prefix="termux-release-tree-",
        dir=os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR"),
    ) as scratch:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(scratch) / "index"))
        cli = "codex-rs/cli/src/main.rs"
        source_cli = git("show", f"{source}:{cli}")
        has_termux_updater = b"async fn run_termux_update()" in source_cli

        def put_blob(path, content):
            blob = git("hash-object", "-w", "--stdin", data=content).decode().strip()
            git("update-index", "--cacheinfo", f"100644,{blob},{path}", env=env)

        def code_tree(ref):
            git("read-tree", ref, env=env)
            for path in excluded:
                present = (
                    subprocess.run(
                        ["git", "cat-file", "-e", f"{upstream}:{path}"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    ).returncode
                    == 0
                )
                if present or git("ls-files", "--", path, env=env):
                    git(
                        "restore",
                        f"--source={upstream}",
                        "--staged",
                        "--",
                        path,
                        env=env,
                    )
            if ref == source:
                # Package version is release metadata, not a compatibility edit.
                path = "codex-rs/Cargo.toml"
                original = git("show", f"{baseline}:{path}")
                current = git("show", f"{source}:{path}")
                version = re.compile(
                    rb'(\[workspace\.package\]\n(?:(?!\[).)*?^version\s*=\s*)"[^"]+"',
                    re.MULTILINE | re.DOTALL,
                )
                match = version.search(original)
                if not match or not version.search(current):
                    raise ValueError(
                        "workspace.package.version missing from patch baseline/source"
                    )
                normalized = version.sub(
                    lambda m: m[1] + match[0][len(match[1]) :], current, count=1
                )
                put_blob(path, normalized)
            if has_termux_updater:
                put_blob(
                    cli, async_updater(git("show", f"{ref}:{cli}").decode()).encode()
                )
            return git("write-tree", env=env).decode().strip()

        base_tree = code_tree(baseline)
        source_tree = code_tree(source)
        upstream_tree = code_tree(upstream)
        merged = subprocess.run(
            [
                "git",
                "-c",
                "merge.conflictStyle=diff3",
                "merge-tree",
                "--write-tree",
                "--name-only",
                "--no-messages",
                f"--merge-base={base_tree}",
                upstream_tree,
                source_tree,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        result = merged.stdout.splitlines()[0] if merged.stdout else ""
        conflicts = merged.stdout.splitlines()[1:]
        if merged.returncode == 1 and has_termux_updater:
            git("read-tree", result, env=env)
            remaining = []
            for path in conflicts:
                if path not in (
                    cli,
                    "codex-rs/tui/src/update_action.rs",
                    "codex-rs/tui/src/updates.rs",
                ):
                    remaining.append(path)
                    continue
                text = merge_updater_additions(git("show", f"{result}:{path}").decode())
                if re.search(r"^(?:<<<<<<<|=======|>>>>>>>)", text, re.MULTILINE):
                    remaining.append(path)
                else:
                    put_blob(path, text.encode())
            if not remaining:
                result = git("write-tree", env=env).decode().strip()
                merged.returncode = 0
        if merged.returncode:
            raise ValueError(
                "Termux delta conflicts with requested upstream; no release refs changed:\n"
                + merged.stdout
                + merged.stderr
            )
        if git("cat-file", "-t", result).strip() != b"tree":
            raise ValueError("release merge did not produce a tree")

        allowed = paths_between(base_tree, source_tree)
        # Git can carry a modified downstream file through an upstream rename.
        changes = git(
            "diff", "--name-status", "-z", "--find-renames", base_tree, upstream
        ).split(b"\0")
        i = 0
        while i < len(changes) and changes[i]:
            status, old = changes[i : i + 2]
            i += 2
            if status.startswith(b"R"):
                new = changes[i]
                i += 1
                if old in allowed:
                    allowed.add(new)
        unexpected = paths_between(upstream, result) - allowed
        if unexpected:
            raise ValueError(
                "release contains changes outside the downstream delta:\n"
                + "\n".join(sorted(p.decode() for p in unexpected))
            )
        validate_manifests(result)
        print(
            f"Verified release tree: {len(paths_between(upstream, result))} downstream paths; "
            "all other paths match requested upstream.",
            file=sys.stderr,
        )
        return result


if __name__ == "__main__":
    try:
        if len(sys.argv) < 4:
            raise ValueError(
                "usage: termux-release-tree.py UPSTREAM SOURCE PAIRED_UPSTREAM [EXCLUDED_PATH ...]"
            )
        print(release_tree(*sys.argv[1:4], [".github", *sys.argv[4:]]))
    except (ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
