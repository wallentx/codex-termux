#!/usr/bin/env python3
"""Validate and claim a release failure before spending an agent session."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = "wallentx/codex-termux"
TAG = re.compile(r"rust-v\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
RELEASE_HEAD = re.compile(r"upstream/rust-v\d+\.\d+\.\d+\Z")
MAX_LOG = 4 * 1024 * 1024


def command(args, *, data=None):
    return subprocess.check_output(args, input=data, text=True, timeout=120).strip()


def api(path, payload=None):
    args = ["gh", "api", path]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    return json.loads(
        command(args, data=json.dumps(payload) if payload is not None else None)
    )


def pages(path, field=None):
    result = []
    for page in range(1, 6):
        separator = "&" if "?" in path else "?"
        data = api(f"{path}{separator}per_page=100&page={page}")
        items = data[field] if field else data
        result.extend(items)
        if len(items) < 100:
            return result
    raise ValueError(
        "API result exceeded 500 items; refusing an incomplete deduplication"
    )


def validate_run(run, mode, pr=None):
    expected = "termux-release-watch" if mode == "watch" else "rust-release"
    if mode not in ("watch", "build"):
        raise ValueError("unknown repair mode")
    if (
        run.get("repository", {}).get("full_name") != REPO
        or run.get("head_repository", {}).get("full_name") != REPO
        or run.get("name") != expected
        or run.get("path") != f".github/workflows/{expected}.yml"
        or not SHA.fullmatch(run.get("head_sha", ""))
    ):
        raise ValueError("run identity, repository, or workflow is not trusted")
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        return False
    if mode == "watch":
        return run.get("head_branch") == "dev" and run.get("event") in (
            "schedule",
            "workflow_dispatch",
            "push",
        )
    if run.get("event") != "pull_request" or not pr:
        return False
    head, base = pr.get("head", {}), pr.get("base", {})
    return (
        pr.get("state") == "open"
        and head.get("repo", {}).get("full_name") == REPO
        and base.get("repo", {}).get("full_name") == REPO
        and bool(RELEASE_HEAD.fullmatch(head.get("ref", "")))
        and base.get("ref") == "release/" + head["ref"].removeprefix("upstream/rust-v")
        and head.get("sha") == run["head_sha"]
        and head.get("ref") == run.get("head_branch")
    )


def upstream_tag(log):
    tags = set(re.findall(r"\bUPSTREAM_TAG:\s*(rust-v[^\s]+)", log))
    if len(tags) != 1 or not TAG.fullmatch(next(iter(tags))):
        raise ValueError("failed watcher log has no unique valid upstream tag")
    return tags.pop()


def redact(log):
    log = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", log)
    log = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED KEY]",
        log,
        flags=re.DOTALL,
    )
    log = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)",
        "[REDACTED TOKEN]",
        log,
    )
    log = re.sub(r"(?i)(authorization:\s*(?:bearer|basic)\s+)\S+", r"\1[REDACTED]", log)
    log = re.sub(
        r"(?im)^.*\b(?:GH_TOKEN|GITHUB_TOKEN|[A-Z_]*(?:API_KEY|PRIVATE_KEY|PASSWORD|SECRET))\s*[:=].*$",
        "[REDACTED CREDENTIAL LINE]",
        log,
    )
    return log


def excerpt(log):
    lines = redact(log).splitlines()
    selected = set(range(max(0, len(lines) - 100), len(lines)))
    matches = [
        i
        for i, line in enumerate(lines)
        if re.search(r"error\[E\d+\]|Termux delta conflicts|error:|FAILED|panic", line)
    ]
    for i in matches[-8:]:
        selected.update(range(max(0, i - 4), min(len(lines), i + 12)))
    return "\n".join(lines[i][:1500] for i in sorted(selected))[-24000:]


def job_log(job_id):
    # Bounded stdout; GitHub's masking is retained and excerpts are redacted again.
    args = ["gh", "api", f"repos/{REPO}/actions/jobs/{int(job_id)}/logs"]
    if "--allow-escape-sequences" in command(["gh", "api", "--help"]):
        # Only capture raw bytes into a pipe; redact strips escapes before use.
        args.append("--allow-escape-sequences")
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
    )
    try:
        data = process.stdout.read(MAX_LOG + 1)
        if len(data) > MAX_LOG:
            raise ValueError(
                "job log exceeds 4 MiB; refusing incomplete failure evidence"
            )
        if process.wait(timeout=60):
            raise ValueError("cannot retrieve failed job log")
        return data.decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def key_for(context):
    identity = [
        context[k] for k in ("mode", "base_sha", "upstream_tag", "patch_source_sha")
    ]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:20]


def output(name, value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("invalid multiline job output")
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(f"{name}={value}\n")


def skip(reason):
    print(reason)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as stream:
            stream.write(f"Repair skipped: {reason}\n")


def prepare(*, dry_run=False):
    output("eligible", "false")
    if os.environ.get("GITHUB_REPOSITORY") != REPO:
        raise ValueError("repair workflow is restricted to its owning repository")
    run_id = os.environ["REPAIR_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("run_id must be a positive integer")
    mode = os.environ["REPAIR_MODE"]
    run = api(f"repos/{REPO}/actions/runs/{run_id}")
    # Validate the provenance before using even the commit ID in another API URL.
    validate_run(run, mode)
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        return skip("run has not completed with a failure")
    pr = None
    if mode == "build":
        candidates = pages(f"repos/{REPO}/commits/{run['head_sha']}/pulls")
        candidates = [p for p in candidates if validate_run(run, "build", p)]
        if len(candidates) != 1:
            return skip("failed commit has no unique current open release PR")
        pr = candidates[0]
    if not validate_run(run, mode, pr):
        return skip(
            "run is successful, stale, or outside the supported release branches"
        )
    attempt = int(run["run_attempt"])
    jobs = pages(f"repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    failed = [j for j in jobs if j["conclusion"] == "failure"]
    if mode == "watch":
        failed = [j for j in failed if j["name"] == "create-release-pr"]
    else:
        failed = [
            j
            for j in failed
            if "android" in j["name"].lower() or "termux" in j["name"].lower()
        ]
    if not failed:
        return skip("no supported failed release job")
    workflow_sha = run["head_sha"]
    if pr:
        # The run head is the PR head, but local reusable workflows record the
        # merge commit whose workflow files GitHub actually executed.
        merge_ref = f"refs/pull/{pr['number']}/merge"
        workflow_shas = set()
        for workflow in run.get("referenced_workflows", []):
            if workflow.get("ref") != merge_ref or not workflow.get(
                "path", ""
            ).startswith(f"{REPO}/.github/workflows/"):
                continue
            sha = workflow.get("sha", "")
            if not SHA.fullmatch(sha) or not workflow["path"].endswith(f"@{sha}"):
                raise ValueError("invalid recorded workflow commit")
            workflow_shas.add(sha)
        if len(workflow_shas) != 1:
            raise ValueError("failed PR run has no unique recorded workflow commit")
        workflow_sha = workflow_shas.pop()
    logs = [job_log(job["id"]) for job in failed[:3]]
    context = {
        "mode": mode,
        "run_id": int(run_id),
        "attempt": attempt,
        "failed_head_sha": run["head_sha"],
        "failed_workflow_sha": workflow_sha,
        "failed_workflow_path": run["path"],
        "failed_jobs": [j["id"] for j in failed[:3]],
        "base_branch": "dev" if mode == "watch" else pr["head"]["ref"],
        "base_sha": command(["git", "rev-parse", "HEAD"])
        if mode == "watch"
        else run["head_sha"],
        "pr_number": 0 if mode == "watch" else pr["number"],
        "upstream_tag": "",
        "patch_source_sha": "",
        "baseline_tag": "",
    }
    if mode == "watch":
        if not any(
            "Termux delta conflicts with requested upstream" in log for log in logs
        ):
            return skip(
                "watcher failure is not a release-tree conflict; investigate infrastructure manually"
            )
        context["upstream_tag"] = upstream_tag("\n".join(logs))
        open_prs = pages(f"repos/{REPO}/pulls?state=open")
        if any(
            f"`{context['upstream_tag']}`" in (p.get("body") or "") for p in open_prs
        ):
            return skip("a release PR for this upstream tag already exists")
        source = api(f"repos/{REPO}/git/ref/heads/wallentx/termux-target")["object"][
            "sha"
        ]
        if not SHA.fullmatch(source):
            raise ValueError("invalid downstream commit")
        command(["git", "fetch", "--no-tags", "origin", source])
        tags = command(
            [
                "git",
                "tag",
                "--merged",
                source,
                "--list",
                "rust-v*-termux",
                "--sort=-v:refname",
            ]
        ).splitlines()
        if not tags:
            raise ValueError("downstream has no paired tested baseline")

        # Use the generator's older-line fallback when target has moved ahead.
        def release_line(tag):
            return tuple(map(int, tag.removeprefix("rust-v").split(".")[:2]))

        if release_line(tags[0]) > release_line(context["upstream_tag"]):
            tags = [
                t
                for t in tags
                if release_line(t) <= release_line(context["upstream_tag"])
            ]
            if not tags:
                raise ValueError(
                    "no tested downstream baseline at or before upstream line"
                )
            source = command(["git", "rev-parse", tags[0]])
        context["patch_source_sha"] = source
        context["baseline_tag"] = tags[0].removesuffix("-termux")
    if dry_run:
        print(json.dumps(context, indent=2))
        return
    key = key_for(context)
    scope = (
        f"{mode}:{context['upstream_tag'] if mode == 'watch' else context['pr_number']}"
    )
    scope_marker = f"<!-- termux-repair-scope:{scope} -->"
    title = f"[termux-repair:{key}] {mode} failure"
    issues = pages(f"repos/{REPO}/issues?state=all&labels=termux-repair")
    existing = [
        issue for issue in issues if issue["title"].startswith(f"[termux-repair:{key}]")
    ]
    if existing and os.environ.get("REPAIR_RETRY") != "true":
        return skip(
            f"failure already claimed by #{existing[0]['number']}; use manual retry to spend another session"
        )
    scoped = [issue for issue in issues if scope_marker in (issue.get("body") or "")]
    if len(scoped) >= 2 and os.environ.get("REPAIR_RETRY") != "true":
        return skip(
            "two automatic attempts already used for this release; inspect the repair issues before retrying"
        )
    if existing:
        issue = existing[0]
    else:
        issue = api(
            f"repos/{REPO}/issues",
            {
                "title": title,
                "labels": ["termux-repair"],
                "body": (
                    f"{scope_marker}\nAutomatic repair attempt for {run['html_url']} (attempt {attempt}).\n\n"
                    f"Base: `{context['base_branch']}` at `{context['base_sha']}`.\n"
                    f"Upstream tag: `{context['upstream_tag'] or 'release PR'}`.\n\n"
                    f"Repair run: https://github.com/{REPO}/actions/runs/{os.environ['GITHUB_RUN_ID']}\n\n"
                    "One automatic agent session is allowed for this fingerprint. Repeated scheduled "
                    "failures are suppressed, including after this issue is closed. A maintainer may "
                    "dispatch the repair workflow with `retry=true` after inspecting a failed attempt.\n\n"
                    "The agent may propose a tested fix or update an existing release PR; it cannot merge or deploy."
                ),
            },
        )
    context.update(key=key, issue=issue["number"])
    directory = Path(os.environ["RUNNER_TEMP"]) / "termux-repair-context"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "repair-context.json").write_text(json.dumps(context, indent=2) + "\n")
    (directory / "failure.log").write_text("\n\n".join(excerpt(log) for log in logs))
    (directory / "control.py").write_text(Path(__file__).read_text())
    for name in (
        "base_sha",
        "base_branch",
        "pr_number",
        "issue",
        "key",
        "upstream_tag",
        "patch_source_sha",
        "baseline_tag",
    ):
        output(name, context[name])
    output("eligible", "true")
    print(f"Claimed {mode} failure as issue #{issue['number']}")


def bootstrap(directory):
    context = json.loads((directory / "repair-context.json").read_text())
    if not SHA.fullmatch(context["base_sha"]):
        raise ValueError("invalid base commit")
    if command(["git", "rev-parse", "HEAD"]) != context["base_sha"]:
        raise ValueError("checkout does not match the validated repair base")
    workflow_sha = context["failed_workflow_sha"]
    workflow_path = context["failed_workflow_path"]
    workflow_names = {"watch": "termux-release-watch", "build": "rust-release"}
    if (
        not SHA.fullmatch(workflow_sha)
        or context["mode"] not in workflow_names
        or workflow_path != f".github/workflows/{workflow_names[context['mode']]}.yml"
    ):
        raise ValueError("invalid failed workflow source")
    command(["git", "fetch", "--no-tags", "origin", workflow_sha])
    failed_workflow = command(["git", "show", f"{workflow_sha}:{workflow_path}"])
    # gh-aw restores trusted .github files from dev. Preserve that boundary and
    # expose the failed revision separately as evidence, never as executable config.
    (directory / "failed-workflow.yml").write_text(failed_workflow + "\n")
    if context["mode"] == "watch":
        source = context["patch_source_sha"]
        if not SHA.fullmatch(source):
            raise ValueError("invalid patch source")
        command(["git", "fetch", "--no-tags", "origin", source])
        for tag in (context["upstream_tag"], context["baseline_tag"]):
            if not TAG.fullmatch(tag):
                raise ValueError("invalid upstream tag")
            command(
                [
                    "git",
                    "fetch",
                    "--no-tags",
                    "https://github.com/openai/codex.git",
                    f"refs/tags/{tag}:refs/tags/{tag}",
                ]
            )
    print("Validated repair context and fetched pinned source refs")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "plan":
            prepare(dry_run=True)
        elif sys.argv[1] == "prepare":
            prepare()
        elif sys.argv[1] == "bootstrap":
            bootstrap(Path(sys.argv[2]))
        else:
            raise ValueError("unknown command")
    except (ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
