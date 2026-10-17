"""Offline provenance, deduplication, and publication-scope regression tests."""

import copy
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "repair", Path(__file__).with_name("termux-repair.py")
)
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)
BASE = "a" * 40
SOURCE = "b" * 40


def run_fixture(mode="watch"):
    name = "termux-release-watch" if mode == "watch" else "rust-release"
    return {
        "id": 42,
        "run_attempt": 1,
        "name": name,
        "path": f".github/workflows/{name}.yml",
        "repository": {"full_name": repair.REPO, "fork": True},
        "head_repository": {"full_name": repair.REPO, "fork": True},
        "status": "completed",
        "conclusion": "failure",
        "head_sha": BASE,
        "head_branch": "dev" if mode == "watch" else "upstream/rust-v0.164.0",
        "event": "schedule" if mode == "watch" else "pull_request",
        "html_url": f"https://github.com/{repair.REPO}/actions/runs/42",
    }


def pr_fixture():
    return {
        "number": 515,
        "state": "open",
        "head": {
            "repo": {"full_name": repair.REPO},
            "ref": "upstream/rust-v0.164.0",
            "sha": BASE,
        },
        "base": {"repo": {"full_name": repair.REPO}, "ref": "release/0.164.0"},
    }


class PolicyTests(unittest.TestCase):
    def test_build_publication_does_not_require_app_administration_access(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / ".github/workflows/termux-build-repair.md").read_text()
        compiled = (root / ".github/workflows/termux-build-repair.lock.yml").read_text()
        self.assertIn("check-branch-protection: false", source)
        configs = [
            json.loads(json.loads(line.split(":", 1)[1].strip()))
            for line in compiled.splitlines()
            if line.strip().startswith("GH_AW_SAFE_OUTPUTS_HANDLER_CONFIG:")
        ]
        self.assertTrue(configs)
        for config in configs:
            self.assertFalse(
                config["push_to_pull_request_branch"]["check_branch_protection"]
            )
        self.assertFalse("permission-administration:" in compiled)
        self.assertFalse("permission-workflows:" in compiled)
        self.assertIn("permission-contents: write", compiled)

    def test_workflows_supply_the_preparation_script_environment(self):
        root = Path(__file__).resolve().parents[2]
        for mode in ("watch", "build"):
            text = (root / f".github/workflows/termux-{mode}-repair.md").read_text()
            keys = set(re.findall(r"^\s+(REPAIR_[A-Za-z_]+):", text, re.MULTILINE))
            self.assertEqual(keys, {"REPAIR_MODE", "REPAIR_RUN_ID", "REPAIR_RETRY"})
            self.assertRegex(text, rf"(?m)^\s+REPAIR_MODE: {mode}$")

    def test_own_fork_is_supported_but_foreign_heads_are_rejected(self):
        run = run_fixture()
        self.assertTrue(repair.validate_run(run, "watch"))
        run["head_repository"]["full_name"] = "untrusted/codex-termux"
        with self.assertRaises(ValueError):
            repair.validate_run(run, "watch")

    def test_workflow_identity_is_checked_before_repair(self):
        for field, value in (
            ("name", "other"),
            ("path", ".github/workflows/other.yml"),
            ("head_sha", "../main"),
        ):
            run = run_fixture()
            run[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                repair.validate_run(run, "watch")

    def test_success_or_untrusted_watch_branch_is_ignored(self):
        for field, value in (
            ("status", "in_progress"),
            ("conclusion", "success"),
            ("head_branch", "wallentx/repair-x"),
            ("event", "pull_request"),
        ):
            run = run_fixture()
            run[field] = value
            self.assertFalse(repair.validate_run(run, "watch"))

    def test_build_only_accepts_current_same_repo_release_pr(self):
        run, pr = run_fixture("build"), pr_fixture()
        self.assertTrue(repair.validate_run(run, "build", pr))
        for mutate in (
            lambda p: p["head"].update(sha=SOURCE),
            lambda p: p["head"]["repo"].update(full_name="outside/repo"),
            lambda p: p["head"].update(ref="wallentx/repair-x"),
            lambda p: p["base"].update(ref="dev"),
            lambda p: p.update(state="closed"),
        ):
            changed = copy.deepcopy(pr)
            mutate(changed)
            self.assertFalse(repair.validate_run(run, "build", changed))

    def test_upstream_tag_must_be_unique_and_not_shell_or_ref_syntax(self):
        tag = "rust-v0.164.0-alpha.3"
        self.assertEqual(
            repair.upstream_tag(f"UPSTREAM_TAG: {tag}\nUPSTREAM_TAG: {tag}"), tag
        )
        for value in (
            "",
            "UPSTREAM_TAG: rust-v0.164.0;touch",
            "UPSTREAM_TAG: rust-v0.164.0/../../dev",
            "UPSTREAM_TAG: rust-v0.164.0\nUPSTREAM_TAG: rust-v0.165.0",
        ):
            with self.assertRaises(ValueError):
                repair.upstream_tag(value)

    def test_logs_are_bounded_and_credentials_redacted(self):
        log = "noise\n" * 10000 + "error[E0599]: failure\n"
        log += "GH_TOKEN: ghp_sensitive\nAuthorization: Bearer sensitive\n"
        log += (
            "-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----\n"
        )
        log += "token github_pat_sensitive and sk-sensitive\n"
        result = repair.excerpt(log)
        self.assertIn("error[E0599]", result)
        self.assertLessEqual(len(result), 24000)
        self.assertNotIn("sensitive", result)
        self.assertNotIn("private material", result)

    def test_fingerprint_ignores_repeated_run_id_but_tracks_source_changes(self):
        context = {
            "mode": "watch",
            "base_sha": BASE,
            "upstream_tag": "rust-v0.164.0",
            "patch_source_sha": SOURCE,
            "run_id": 1,
        }
        first = repair.key_for(context)
        context["run_id"] = 2
        self.assertEqual(repair.key_for(context), first)
        context["patch_source_sha"] = "c" * 40
        self.assertNotEqual(repair.key_for(context), first)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.issues = []
        self.posts = []
        self.env = {
            "GITHUB_OUTPUT": str(self.output),
            "GITHUB_REPOSITORY": repair.REPO,
            "GITHUB_RUN_ID": "100",
            "RUNNER_TEMP": str(self.root),
            "REPAIR_RUN_ID": "42",
            "REPAIR_RETRY": "false",
            "REPAIR_MODE": "watch",
        }
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env).start()
        patch.object(repair, "api", self.api).start()
        patch.object(repair, "pages", self.pages).start()
        patch.object(repair, "command", self.command).start()
        patch.object(
            repair,
            "job_log",
            return_value="UPSTREAM_TAG: rust-v0.164.0-alpha.3\nTermux delta conflicts with requested upstream",
        ).start()

    def api(self, path, payload=None):
        if payload is not None:
            self.posts.append((path, payload))
            issue = dict(payload, number=100 + len(self.posts))
            self.issues.append(issue)
            return issue
        if "/actions/runs/" in path:
            return run_fixture()
        if "/git/ref/" in path:
            return {"object": {"sha": SOURCE}}
        raise AssertionError(path)

    def pages(self, path, field=None):
        if path.endswith("/jobs"):
            return [{"id": 10, "name": "create-release-pr", "conclusion": "failure"}]
        if "/issues?" in path:
            return self.issues
        if "/pulls?" in path:
            return []
        raise AssertionError(path)

    def command(self, args, **kwargs):
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return BASE
        if args[:2] == ["git", "tag"]:
            return "rust-v0.163.0-termux"
        if args[:2] == ["git", "fetch"]:
            return ""
        raise AssertionError(args)

    def execute(self, **kwargs):
        self.output.write_text("")
        with redirect_stdout(io.StringIO()):
            repair.prepare(**kwargs)
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def test_claim_once_and_repeated_schedule_does_not_spend_another_session(self):
        first = self.execute()
        self.assertEqual(first["eligible"], "true")
        context = json.loads(
            (self.root / "termux-repair-context/repair-context.json").read_text()
        )
        self.assertEqual(
            (context["base_sha"], context["patch_source_sha"], context["baseline_tag"]),
            (BASE, SOURCE, "rust-v0.163.0"),
        )
        second = self.execute()
        self.assertEqual(second, {"eligible": "false"})
        self.assertEqual(len(self.posts), 1)

    def test_two_attempt_budget_and_explicit_manual_retry(self):
        self.issues = [
            {
                "title": f"[termux-repair:old-{i}]",
                "number": i,
                "body": "<!-- termux-repair-scope:watch:rust-v0.164.0-alpha.3 -->",
            }
            for i in (1, 2)
        ]
        self.assertEqual(self.execute(), {"eligible": "false"})
        self.assertEqual(self.posts, [])
        os.environ["REPAIR_RETRY"] = "true"
        self.assertEqual(self.execute()["eligible"], "true")
        self.assertEqual(len(self.posts), 1)

    def test_manual_retry_reuses_existing_issue(self):
        self.execute()
        os.environ["REPAIR_RETRY"] = "true"
        self.assertEqual(self.execute()["eligible"], "true")
        self.assertEqual(len(self.posts), 1)

    def test_plan_is_read_only(self):
        self.assertEqual(self.execute(dry_run=True), {"eligible": "false"})
        self.assertEqual(self.posts, [])
        self.assertFalse((self.root / "termux-repair-context").exists())


if __name__ == "__main__":
    unittest.main()
