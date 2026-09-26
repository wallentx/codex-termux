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


if __name__ == "__main__":
    unittest.main()
