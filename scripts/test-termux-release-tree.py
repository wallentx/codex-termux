#!/usr/bin/env python3
"""Git-only release regression tests; never invoke Cargo or GitHub."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts/termux-release-tree.py"
spec = importlib.util.spec_from_file_location("release_tree", HELPER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReleaseTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Termux Release Test")
        self.git("config", "user.email", "termux-release-test@example.invalid")
        # Fixture commits must not depend on the developer's signing agent.
        self.git("config", "commit.gpgsign", "false")
        self.write(
            "codex-rs/Cargo.toml",
            '[workspace]\nmembers = ["cli"]\n[workspace.package]\nversion = "1.0.0-alpha.1"\n[workspace.dependencies]\n',
        )
        self.write(
            "codex-rs/Cargo.lock",
            '[[package]]\nname = "codex-cli"\nversion = "0.0.0"\n',
        )
        self.write(
            "codex-rs/cli/Cargo.toml",
            '[package]\nname = "codex-cli"\nversion.workspace = true\n',
        )
        self.write("codex-rs/cli/src/main.rs", "fn main() {}\n")
        self.write(
            "src/shared", "upstream one\n" + "padding\n" * 10 + "platform default\n"
        )
        self.write("src/alpha-only", "alpha feature\n")
        self.write(".github/workflows/test.yml", "upstream automation\n")
        self.baseline = self.commit("upstream alpha")
        self.git("tag", "rust-v1.0.0-alpha.1")
        self.write(
            "src/shared", "upstream one\n" + "padding\n" * 10 + "Termux platform\n"
        )
        self.write(".github/workflows/test.yml", "downstream automation\n")
        self.source = self.commit("Termux compatibility")
        self.git("tag", "rust-v1.0.0-alpha.1-termux")
        self.git("checkout", "--detach", self.baseline)
        self.git("rm", "src/alpha-only")
        self.write(
            "src/shared", "upstream two\n" + "padding\n" * 10 + "platform default\n"
        )
        self.write(
            "codex-rs/Cargo.toml",
            (self.repo / "codex-rs/Cargo.toml")
            .read_text()
            .replace("1.0.0-alpha.1", "1.0.0"),
        )
        self.upstream = self.commit("stable release excludes alpha code")
        self.git("tag", "rust-v1.0.0")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, stderr=subprocess.DEVNULL, text=True
        ).strip()

    def write(self, path, content):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def tree(self, success=True):
        result = subprocess.run(
            ["python3", str(HELPER), self.upstream, self.source, self.baseline],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result.stdout.strip() if success else result.stderr

    def test_stable_keeps_only_downstream_delta(self):
        self.write("user-work", "keep staged edits\n")
        self.git("add", "user-work")
        index = self.git("write-tree")
        head = self.git("rev-parse", "HEAD")
        tree = self.tree()
        self.assertEqual(self.git("write-tree"), index)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(
            self.git("show", f"{tree}:src/shared"),
            "upstream two\n" + "padding\n" * 10 + "Termux platform",
        )
        self.assertNotIn(
            "src/alpha-only",
            self.git("ls-tree", "-r", "--name-only", tree).splitlines(),
        )
        self.assertEqual(
            self.git("show", f"{tree}:.github/workflows/test.yml"),
            "upstream automation",
        )
        self.assertEqual(self.tree(), tree)

    def test_real_conflict_fails_without_checkout_changes(self):
        self.write(
            "src/shared", "upstream two\n" + "padding\n" * 10 + "different platform\n"
        )
        self.upstream = self.commit("conflicting platform change")
        before = self.git("write-tree")
        self.assertIn("src/shared", self.tree(False))
        self.assertEqual(self.git("write-tree"), before)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_adjacent_workspace_dependencies_merge_without_losing_either_side(self):
        self.git("checkout", "--detach", self.baseline)
        workspace = (self.repo / "codex-rs/Cargo.toml").read_text()
        workspace += """codex-utils-absolute-path = "0.0.0"
codex-utils-file-lock = "0.0.0"
codex-utils-output-truncation = "0.0.0"
codex-utils-path = "0.0.0"
"""
        self.write("codex-rs/Cargo.toml", workspace)
        manifest = """[package]
name = "codex-cli"
version.workspace = true
[dependencies]
codex-utils-absolute-path = { workspace = true }
codex-utils-path = { workspace = true }
"""
        lock = """[[package]]
name = "codex-cli"
version = "0.0.0"
dependencies = [
 "codex-utils-absolute-path",
 "codex-utils-path",
]
"""
        self.write("codex-rs/cli/Cargo.toml", manifest)
        self.write("codex-rs/Cargo.lock", lock)
        self.baseline = self.commit("dependency baseline")

        self.write(
            "codex-rs/cli/Cargo.toml",
            manifest.replace(
                "codex-utils-path =",
                "codex-utils-file-lock = { workspace = true }\ncodex-utils-path =",
            ),
        )
        self.write(
            "codex-rs/Cargo.lock",
            lock.replace(
                ' "codex-utils-path",',
                ' "codex-utils-file-lock",\n "codex-utils-path",',
            ),
        )
        self.source = self.commit("Termux file lock")

        self.git("checkout", "--detach", self.baseline)
        self.write(
            "codex-rs/cli/Cargo.toml",
            manifest.replace(
                "codex-utils-path =",
                "codex-utils-output-truncation = { workspace = true }\ncodex-utils-path =",
            ),
        )
        self.write(
            "codex-rs/Cargo.lock",
            lock.replace(
                ' "codex-utils-path",',
                ' "codex-utils-output-truncation",\n "codex-utils-path",',
            ),
        )
        self.upstream = self.commit("upstream output truncation")
        tree = self.tree()
        self.assertIn(
            "codex-utils-file-lock = { workspace = true }\n"
            "codex-utils-output-truncation = { workspace = true }",
            self.git("show", f"{tree}:codex-rs/cli/Cargo.toml"),
        )
        self.assertIn(
            ' "codex-utils-file-lock",\n "codex-utils-output-truncation",',
            self.git("show", f"{tree}:codex-rs/Cargo.lock"),
        )
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_upstream_rename_preserves_termux_edit(self):
        self.git("mv", "src/shared", "src/renamed")
        self.upstream = self.commit("move platform source")
        self.assertIn("Termux platform", self.git("show", f"{self.tree()}:src/renamed"))

    def test_source_version_does_not_downgrade_release(self):
        self.git("checkout", "--detach", self.source)
        self.write(
            "codex-rs/Cargo.toml",
            (self.repo / "codex-rs/Cargo.toml")
            .read_text()
            .replace("1.0.0-alpha.1", "0.0.0-local"),
        )
        self.source = self.commit("local version metadata")
        self.assertIn(
            'version = "1.0.0"', self.git("show", f"{self.tree()}:codex-rs/Cargo.toml")
        )

    def test_missing_inherited_dependency_fails_preflight(self):
        self.git("checkout", "--detach", self.source)
        self.write(
            "codex-rs/cli/Cargo.toml",
            '[package]\nname = "codex-cli"\n[dependencies]\nrand_regex.workspace = true\n',
        )
        self.source = self.commit("invalid dependency")
        self.assertIn("missing workspace dependency rand_regex", self.tree(False))

    def test_generator_publication_and_unchanged_retry(self):
        origin = self.root / "origin.git"
        self.git("init", "--bare", str(origin))
        self.git("remote", "add", "origin", str(origin))
        self.git("remote", "add", "upstream", str(origin))
        self.git(
            "push",
            "origin",
            f"{self.source}:refs/heads/wallentx/termux-target",
            "--tags",
        )
        self.git("fetch", "origin")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text("""#!/usr/bin/env bash
set -euo pipefail
case "$1 $2" in
  'pr list')
    if [[ "${TEST_OPEN_PR:-}" == 1 ]]; then
      printf '%s\\n' '[{"number":1,"title":"Termux rust-v1.0.0","body":"- Upstream tag: `rust-v1.0.0`\\n- Release train branch: `release/1.0.0`","headRefName":"wallentx/release-test","baseRefName":"release/1.0.0","url":"https://github.com/wallentx/codex-termux/pull/1","state":"OPEN"}]'
    else
      echo '[]'
    fi ;;
  'pr view') echo '{"headRefOid":"test-head","mergeStateStatus":"CLEAN","mergeable":"MERGEABLE","state":"OPEN"}' ;;
  'pr create') echo 'https://github.com/wallentx/codex-termux/pull/1' ;;
  'pr edit'|'pr merge'|'label create') ;;
  'release view') exit 1 ;;
  *) echo "Unexpected GitHub call: $*" >&2; exit 1 ;;
esac
""")
        gh.chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            GITHUB_REPOSITORY="wallentx/codex-termux",
            UPSTREAM_REPO="openai/codex",
            UPSTREAM_TAG="rust-v1.0.0",
            UPSTREAM_NAME="1.0.0",
            UPSTREAM_HTML_URL="https://github.com/openai/codex/releases/tag/rust-v1.0.0",
            UPSTREAM_PRERELEASE="false",
            UPSTREAM_TARGET="main",
            UPSTREAM_ID="1",
            UPSTREAM_BODY="fixture",
            RELEASE_TRAIN="1.0.0",
            RELEASE_BRANCH="release/1.0.0",
            WORK_BRANCH="wallentx/release-test",
            TERMUX_TAG="rust-v1.0.0-termux",
            PATCH_BRANCH="wallentx/termux-target",
            REVIEWER="wallentx",
            RUNNER_TEMP=str(self.root),
            GITHUB_OUTPUT=str(self.root / "outputs"),
            GH_TOKEN="fixture",
        )
        # Fresh runners may not have the historical upstream tag locally.
        self.git("tag", "-d", "rust-v1.0.0-alpha.1")
        for attempt in range(2):
            if attempt:
                env["TEST_OPEN_PR"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/termux-create-release-pr.sh")],
                cwd=self.repo,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            head = self.git("rev-parse", "origin/wallentx/release-test")
            if not attempt:
                first = head
            else:
                self.assertEqual(head, first)
                self.assertIn("leaving its commit and CI intact", result.stdout)
        self.assertNotIn(
            "src/alpha-only",
            self.git("ls-tree", "-r", "--name-only", head).splitlines(),
        )
        self.assertIn("Termux platform", self.git("show", f"{head}:src/shared"))
        self.assertEqual(
            self.git("show", "origin/release/1.0.0:src/shared"),
            self.git("show", f"{self.upstream}:src/shared"),
        )
        # Advancing the target to the next alpha must retain the older tested
        # source for this stable line, including when its PR already exists.
        # The no-push retry leaves the verified tree staged on the release base.
        self.git("checkout", "--detach", head)
        self.git("checkout", "--detach", self.source)
        self.write("src/next-alpha-only", "must not enter stable\n")
        advanced = self.commit("next alpha target")
        self.git("tag", "rust-v2.0.0-alpha.1-termux", advanced)
        self.git("push", "origin", f"{advanced}:refs/heads/wallentx/termux-target")
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/termux-create-release-pr.sh")],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("has advanced to rust-v2.0.0-alpha.1-termux", result.stdout)
        self.assertEqual(self.git("rev-parse", "origin/wallentx/release-test"), head)

        # A conflicting retry must not replace either already-published ref.
        before = self.git("ls-remote", "--heads", "origin")
        self.git("checkout", "--detach", head)
        self.git("checkout", "--detach", self.upstream)
        self.write(
            "src/shared",
            "upstream two\n" + "padding\n" * 10 + "incompatible platform\n",
        )
        conflicting = self.commit("conflicting upstream")
        self.git("tag", "rust-v1.0.1", conflicting)
        env["UPSTREAM_TAG"] = "rust-v1.0.1"
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/termux-create-release-pr.sh")],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Termux delta conflicts", result.stderr)
        self.assertEqual(self.git("ls-remote", "--heads", "origin"), before)
        self.git("tag", "-d", "rust-v1.0.0-alpha.1-termux")
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/termux-create-release-pr.sh")],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No tested Termux tag at or before release line", result.stderr)
        self.assertEqual(self.git("ls-remote", "--heads", "origin"), before)


class RedesignedUpstreamAdaptationTests(unittest.TestCase):
    def apply(self, text, edits):
        for before, after, _ in edits:
            text = text.replace(before, after)
        return text

    def test_arg0_lock_fallback_follows_new_drop_order(self):
        baseline = (
            "use codex_sandboxing::landlock::CODEX_LINUX_SANDBOX_ARG0;\n"
            "    _temp_dir: TempDir,\n    _lock_file: File,\n"
            "    fn new(temp_dir: TempDir, lock_file: File, paths: P) {}\n"
            "    lock_file.try_lock()?;\n\n"
            "    match lock_file.try_lock() {\n"
            "        Ok(()) => Ok(Some(lock_file)),\n"
            "        Err(std::fs::TryLockError::WouldBlock) => Ok(None),\n"
            "        Err(err) => Err(err.into()),\n    }\n"
            "            lock_file,\n            Arg0DispatchPaths {\n"
            "            lock_file,\n            Arg0DispatchPaths {\n"
            "        lock_file.try_lock()?;\n"
        )
        source = self.apply(baseline, module.ARG0_LOCK_EDITS)
        upstream = baseline.replace(
            "    _temp_dir: TempDir,\n    _lock_file: File,\n",
            "    // drop order\n    _lock_file: File,\n    _temp_dir: TempDir,\n",
        )
        adapted = module.adapt_arg0_lock(baseline, source, upstream)
        self.assertEqual(
            adapted,
            self.apply(upstream, module.ARG0_LOCK_EDITS),
        )
        self.assertIn(
            "    _lock_file: Option<File>,\n    _temp_dir: TempDir,\n", adapted
        )
        self.assertEqual(module.adapt_arg0_lock(upstream, adapted, upstream), adapted)
        for unsafe in (source + "extra\n", source.replace("Some(lock_file)", "None")):
            with self.subTest(source=unsafe), self.assertRaises(ValueError):
                module.adapt_arg0_lock(baseline, unsafe, upstream)

    def test_android_package_validation_follows_entrypoint_signature(self):
        old = "fn validate_package(root: &Path) {}\n"
        new = "fn validate_package(root: &Path, entrypoint: &str) {}\n"
        tail = module._RG_REQUIRED + (
            '        ("macos", "aarch64") => Ok("aarch64-apple-darwin"),\n'
        )
        baseline = old + tail
        source = old + self.apply(tail, module.ANDROID_PACKAGE_EDITS)
        upstream = new + tail
        self.assertEqual(
            module.adapt_android_package_validation(baseline, source, upstream),
            new + self.apply(tail, module.ANDROID_PACKAGE_EDITS),
        )
        with self.assertRaises(ValueError):
            module.adapt_android_package_validation(
                baseline, source + "// extra\n", upstream
            )
        self.assertEqual(
            module.adapt_android_package_validation(baseline, source, baseline),
            source,
        )

    def test_writer_lock_fallback_follows_bounded_coordination_wait(self):
        baseline = (
            "use codex_protocol::ThreadId;\n"
            "    file: Option<File>,\n}\n" + module._WRITER_LOCK_BASE_COORDINATION
        )
        source = self.apply(baseline, module.WRITER_LOCK_EDITS[:2]).replace(
            module._WRITER_LOCK_BASE_COORDINATION,
            module._WRITER_LOCK_SOURCE_COORDINATION,
        )
        upstream = baseline.replace(
            module._WRITER_LOCK_BASE_COORDINATION,
            module._WRITER_LOCK_UPSTREAM_COORDINATION,
        )
        # Only the anchors present in this reduced fixture are exercised here.
        with self.assertRaises(ValueError):
            module.adapt_writer_lock_timeout(baseline, source, upstream)
        # Already-adapted or unchanged baselines stay inactive.
        self.assertEqual(
            module.adapt_writer_lock_timeout(baseline, source, baseline), source
        )
        self.assertEqual(
            module.adapt_writer_lock_timeout(baseline, baseline, upstream), baseline
        )

    def test_exact_refs_preserve_writer_lock_and_update_test(self):
        refs = (
            "rust-v0.166.0-alpha.2",
            "8c7891c720710563d33bf74e83aad10762f48c42",
            "rust-v0.165.0-alpha.1",
        )
        path = "codex-rs/rollout/src/writer_lock.rs"
        try:
            upstream, source, baseline = (
                subprocess.check_output(
                    ["git", "show", f"{ref}:{path}"],
                    cwd=ROOT,
                    stderr=subprocess.DEVNULL,
                ).decode()
                for ref in refs
            )
        except subprocess.CalledProcessError:
            self.skipTest("exact downstream refs are not available")
        adapted = module.adapt_writer_lock_timeout(baseline, source, upstream)
        self.assertIn(
            "TryLockDirOutcome::Acquired(guard) => Some(Some(guard))", adapted
        )
        self.assertIn("io::ErrorKind::TimedOut", adapted)
        self.assertNotIn("file.lock()", adapted)
        self.assertEqual(
            module.adapt_writer_lock_timeout(upstream, adapted, upstream), adapted
        )
        for unsafe in (source + "// edit\n", source.replace("fallback_lock,", "None,")):
            with self.subTest(source=unsafe), self.assertRaises(ValueError):
                module.adapt_writer_lock_timeout(baseline, unsafe, upstream)

    def test_split_installer_test_keeps_termux_assertion_separate(self):
        header = "    fn standalone_update_commands_rerun_latest_installer() {\n"
        assertion = (
            "        assert_eq!(\n"
            "            UpdateAction::TermuxSelfUpdate.command_args(),\n"
            '            ("codex", &["update"][..])\n'
            "        );\n"
        )
        body = "        assert_eq!(unix);\n"
        path = "codex-rs/tui/src/update_action.rs"
        ours = "    fn standalone_windows_update_command_reruns_latest_installer() {\n"
        conflict = (
            f"<<<<<<< upstream\n{ours}||||||| baseline\n{header}{body}"
            f"=======\n{header}{assertion}{body}>>>>>>> target\n"
        )
        self.assertEqual(
            module.merge_updater_additions(conflict, path=path),
            "    fn termux_self_update_command_runs_codex_update() {\n"
            + assertion
            + "    }\n\n    #[test]\n"
            + ours,
        )
        self.assertEqual(module.merge_updater_additions(conflict), conflict)
        unsafe = conflict.replace(assertion, assertion + "        extra();\n")
        self.assertEqual(module.merge_updater_additions(unsafe, path=path), unsafe)

    def test_exact_refs_preserve_lock_fallback_through_daemon_socket_split(self):
        refs = ("rust-v0.163.0", "063d0b87de1bc4c9ba955355d4476d8f22982ba3")
        path = "codex-rs/app-server-transport/src/transport/unix_socket.rs"
        try:
            baseline, source = (
                subprocess.check_output(
                    ["git", "show", f"{ref}:{path}"],
                    cwd=ROOT,
                    stderr=subprocess.DEVNULL,
                ).decode()
                for ref in refs
            )
        except subprocess.CalledProcessError:
            self.skipTest("exact downstream refs are not available")
        upstream = (
            baseline.replace(
                "pub async fn start_control_socket_acceptor(",
                "pub async fn bind_private_daemon_socket(",
            )
            + "\n"
        )
        adapted = module.adapt_split_daemon_socket(baseline, source, upstream)
        self.assertIn("_lock_dir_guard: lock_dir_guard", adapted)
        self.assertIn("try_acquire_sibling_lock_dir", adapted)
        with self.assertRaises(ValueError):
            module.adapt_split_daemon_socket(baseline, source + "// edit\n", upstream)
        self.assertEqual(
            module.adapt_split_daemon_socket(baseline, source, baseline), source
        )


class RetiredClipboardGuardTests(unittest.TestCase):
    def setUp(self):
        self.baseline = (
            b'#[cfg(not(target_os = "macos"))]\nstruct SuppressStderr;\n\n'
            b'#[cfg(not(target_os = "macos"))]\nimpl SuppressStderr {\n'
            b"    fn new() -> Self { Self }\n}\n"
        )
        self.source = self.baseline.replace(
            b'#[cfg(not(target_os = "macos"))]',
            b'#[cfg(all(not(target_os = "android"), not(target_os = "macos")))]',
        )
        self.upstream = b"pub(crate) mod worker;\n"

    def test_retires_guard_only_when_upstream_removed_helper(self):
        self.assertEqual(
            module.drop_retired_clipboard_guard(
                self.baseline, self.source, self.upstream
            ),
            self.baseline,
        )
        self.assertEqual(
            module.drop_retired_clipboard_guard(
                self.baseline, self.source, self.baseline
            ),
            self.source,
        )

    def test_preserves_other_downstream_edits(self):
        for changed in (
            self.source + b"fn termux_copy() {}\n",
            self.source.replace(b"fn new()", b"fn create()"),
        ):
            with self.subTest(source=changed):
                self.assertEqual(
                    module.drop_retired_clipboard_guard(
                        self.baseline, changed, self.upstream
                    ),
                    changed,
                )

    def test_unrecognized_or_partial_guard_changes_are_not_retired(self):
        for source in (
            self.baseline,
            self.baseline.replace(
                b'#[cfg(not(target_os = "macos"))]',
                b'#[cfg(all(not(target_os = "android"), not(target_os = "macos")))]',
                1,
            ),
            self.source.replace(b"struct SuppressStderr;", b"struct SuppressStderr {}"),
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    module.drop_retired_clipboard_guard(
                        self.baseline, source, self.upstream
                    ),
                    source,
                )


class UpdaterAdaptationTests(unittest.TestCase):
    def test_prompt_constant_removal_preserves_upstream_debug_guard(self):
        declaration = (
            "        let release_notes_url = self.update_action.release_notes_url();\n"
        )
        constant = 'const RELEASE_NOTES_URL: &str = "https://github.com/openai/codex/releases/latest";\n\n'
        guard = "#[cfg(not(debug_assertions))]\n"
        conflict = f"<<<<<<< upstream\n{constant}{guard}||||||| baseline\n{constant}=======\n>>>>>>> target\n{declaration}"
        self.assertEqual(
            module.merge_updater_additions(
                conflict, path="codex-rs/tui/src/update_prompt.rs"
            ),
            guard + declaration,
        )
        changed = conflict.replace(guard, "#[cfg(test)]\n")
        self.assertEqual(
            module.merge_updater_additions(
                changed, path="codex-rs/tui/src/update_prompt.rs"
            ),
            changed,
        )

    def test_prompt_url_edits_preserve_new_upstream_layout(self):
        declaration = (
            "        let release_notes_url = self.update_action.release_notes_url();\n"
        )
        for base, ours in (
            (
                "                RELEASE_NOTES_URL.dim().underlined(),\n            ])\n            .inset(Insets::tlbr(0, 2, 0, 0)),\n",
                "                RELEASE_NOTES_URL.dim().underlined(),\n            ]))\n            .wrap(Wrap { trim: false })\n            .inset(Insets::vh(/*v*/ 0, /*h*/ 2)),\n",
            ),
            (
                "        column.render(area, buf);\n        crate::terminal_hyperlinks::mark_underlined_hyperlink(buf, area, RELEASE_NOTES_URL);\n",
                "        render_menu_surface(panel, buf);\n        column.render(panel, buf);\n        crate::terminal_hyperlinks::mark_underlined_hyperlink(buf, area, RELEASE_NOTES_URL);\n",
            ),
        ):
            with self.subTest(base=base):
                theirs = base.replace("RELEASE_NOTES_URL", "release_notes_url")
                conflict = f"{declaration}<<<<<<< upstream\n{ours}||||||| baseline\n{base}=======\n{theirs}>>>>>>> target\n"
                expected = declaration + ours.replace(
                    "RELEASE_NOTES_URL", "release_notes_url"
                )
                self.assertEqual(
                    module.merge_updater_additions(
                        conflict, path="codex-rs/tui/src/update_prompt.rs"
                    ),
                    expected,
                )
                # The rule must not operate on other files or without the
                # action-specific URL binding, nor discard other target edits.
                self.assertEqual(module.merge_updater_additions(conflict), conflict)
                for unsafe in (
                    conflict.replace(declaration, ""),
                    conflict.replace(theirs, theirs + "        custom_render();\n"),
                    conflict.replace(
                        ours, ours.replace("RELEASE_NOTES_URL", "OTHER_URL")
                    ),
                ):
                    self.assertEqual(
                        module.merge_updater_additions(
                            unsafe, path="codex-rs/tui/src/update_prompt.rs"
                        ),
                        unsafe,
                    )

    def test_known_insertion_preserves_upstream_addition(self):
        ours = "    Daemon,\n"
        theirs = "    /// Update by replacing the current Termux binary from wallentx/codex-termux.\n    TermuxSelfUpdate,\n"
        conflict = f"<<<<<<< upstream\n{ours}||||||| baseline\n=======\n{theirs}>>>>>>> target\n"
        self.assertEqual(module.merge_updater_additions(conflict), theirs + ours)

    def test_nonempty_base_is_not_treated_as_an_insertion(self):
        conflict = '<<<<<<< upstream\n    Daemon,\n||||||| baseline\n    Existing,\n=======\n            UpdateAction::TermuxSelfUpdate => ("codex", &["update"]),\n>>>>>>> target\n'
        self.assertEqual(module.merge_updater_additions(conflict), conflict)

    def test_async_calls_preserve_daemon_arguments(self):
        original = "fn handle_app_exit(\n    exit_info: AppExitInfo,\n) {\n    run_update_action(action, cli_executable)?;\n}\n"
        expected = original.replace("fn handle", "async fn handle").replace(
            "cli_executable)?", "cli_executable).await?"
        )
        self.assertEqual(module.async_updater(original), expected)
        self.assertEqual(module.async_updater(expected), expected)

    def test_unrecognized_conflicts_remain_conflicts(self):
        conflict = "<<<<<<< upstream\nnew\n||||||| baseline\nold\n=======\nTermux\n>>>>>>> target\n"
        self.assertEqual(module.merge_updater_additions(conflict), conflict)


class DependencyAdditionTests(unittest.TestCase):
    def test_android_openssl_survives_deletion_of_a_neighboring_section(self):
        path = "codex-rs/core/Cargo.toml"
        base = (
            "[target.'cfg(unix)'.dependencies]\n"
            "codex-shell-escalation = { workspace = true }\n\n"
        )
        android = (
            "# Build OpenSSL from source for Android builds.\n"
            "[target.aarch64-linux-android.dependencies]\n"
            'openssl-sys = { workspace = true, features = ["vendored"] }\n\n'
        )
        for target in (android + base, base + android):
            with self.subTest(target=target):
                conflict = (
                    f"<<<<<<< upstream\n||||||| baseline\n{base}"
                    f"=======\n{target}>>>>>>> target\n"
                )
                self.assertEqual(
                    module.merge_dependency_additions(conflict, path=path), android
                )
                for unsafe in (
                    conflict.replace(
                        "<<<<<<< upstream\n", "<<<<<<< upstream\nchanged\n"
                    ),
                    conflict.replace(target, target + "another-edit\n"),
                    conflict.replace('features = ["vendored"]', 'features = ["other"]'),
                ):
                    self.assertEqual(
                        module.merge_dependency_additions(unsafe, path=path), unsafe
                    )
                for wrong_path in ("codex-rs/Cargo.lock", "docs/Cargo.toml"):
                    self.assertEqual(
                        module.merge_dependency_additions(conflict, path=wrong_path),
                        conflict,
                    )

    def test_only_disjoint_codex_utils_insertions_are_combined(self):
        for path, upstream, target in (
            (
                "codex-rs/rollout/Cargo.toml",
                "codex-utils-output-truncation = { workspace = true }\n",
                "codex-utils-file-lock = { workspace = true }\n",
            ),
            (
                "codex-rs/Cargo.lock",
                ' "codex-utils-cargo-bin",\n',
                ' "codex-utils-file-lock",\n',
            ),
        ):
            with self.subTest(path=path):
                conflict = (
                    f"<<<<<<< upstream\n{upstream}"
                    f"||||||| baseline\n=======\n{target}>>>>>>> target\n"
                )
                self.assertEqual(
                    module.merge_dependency_additions(conflict, path=path),
                    "".join(sorted((upstream, target))),
                )
                for unsafe in (
                    conflict.replace("||||||| baseline\n", "||||||| baseline\nold\n"),
                    conflict.replace(target, "other-change\n"),
                    conflict.replace(target, upstream),
                ):
                    self.assertEqual(
                        module.merge_dependency_additions(unsafe, path=path),
                        unsafe,
                    )


if __name__ == "__main__":
    unittest.main()
