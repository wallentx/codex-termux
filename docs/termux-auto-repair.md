# Automatic release repair

`termux-repair-dispatch.yml` listens for failures from `termux-release-watch`
and the Android release build/smoke pipeline. It verifies that both the run
and its head repository belong to `wallentx/codex-termux`, then dispatches one
of two GitHub Agentic Workflows:

| Failure | Agent output | Validation |
| --- | --- | --- |
| Release-tree conflict | A repair PR against `dev` | Release-generator tests and the exact failing upstream/patch refs before publication; normal PR regression CI afterward |
| Android build or smoke failure | A scoped commit on the current open release PR | Focused checks by the agent; the existing Android build and smoke workflow starts again after the App-authenticated push |

The dispatcher is separate because gh-aw v0.92.0's generated `workflow_run`
guard excludes repositories whose `fork` flag is true. This repository is a
fork. The dispatcher instead compares repository IDs, and the preparation
script checks the repository, workflow path, event, branch, PR ownership, and
current PR head again. No untrusted fork checkout is executed with write
credentials.

The Android build selects its `rusty-v8-v<version>` release using the same
resolved-version helper as the V8 artifact producer. It downloads the archive,
bindings, and checksums together and verifies both files before exporting their
paths. An upstream V8 bump therefore cannot silently reuse an older artifact.

## Authentication

The agent workflows use `permissions: copilot-requests: write`, as requested.
GitHub must provide organization-backed Copilot inference access to the Actions
token. This is a GitHub account/billing capability, not something the workflow
can grant itself. The keyless path was verified in this repository on 2026-10-17:
[run 39052563860](https://github.com/wallentx/codex-termux/actions/runs/39052563860)
completed inference, validation, threat detection, and publication of PR #522.
No PAT is configured or uploaded by this setup.

The existing `APP_CLIENT_ID` variable and `APP_PRIVATE_KEY` secret are used by
deterministic preparation and publication jobs only. The App needs contents,
issues, and pull requests write access, and actions read access. The build repair
disables gh-aw's optional branch-protection API lookup so it does not request
administration access; GitHub still enforces branch protection when pushing.
Its installation token is scoped to this repository and revoked by the token
action. Copilot
receives a read-only repository token; the framework's separate safe-output job
is the only part that can publish its proposed patch.

Security review of the new workflow dependencies: GitHub-owned checkout,
artifact, GitHub Script, token, and runtime setup actions are pinned to commit
SHAs in the generated lock files. gh-aw setup and sandbox images are also pinned.
The App private key is never forwarded into the agent job. There are no new
workflow redirects or third-party inference credentials. The compiler's built-in
threat detection remains enabled. The only new inference permission is
`copilot-requests: write`.

## Limits and retries

- One automatic session per failure fingerprint; repeated scheduled failures
  reuse the existing `termux-repair` issue rather than launching more agents.
- At most two automatic sessions per upstream tag or release PR, including
  closed issues. Each agent job has a 30-minute timeout and 40-turn cap.
- Watcher repairs can change only the release-tree helper, focused test scripts,
  and release documentation. Build repairs can change only Rust sources and
  Cargo manifests/lockfile, on the validated current release PR.
- Patch limits are 20 files and 256 KiB. Workflows, agent instructions, and
  other protected files cannot be changed through either publishing capability.
- Publication requires successful post-agent checks, including changed-file
  formatting, the exact release preflight, and the existing executable
  startup-lock regressions for watcher repairs.
- Agents cannot merge, approve, deploy, or bypass failed checks. A watcher fix
  still needs PR review; the normal scheduled watcher resumes after it is merged.

An exhausted or unsuccessful attempt leaves its issue with a link to the repair
run. After inspecting it, a maintainer can explicitly spend another session:

```sh
gh workflow run termux-watch-repair.lock.yml --repo wallentx/codex-termux --ref dev -f run_id=FAILED_RUN_ID -f retry=true
# For an Android release PR failure, use termux-build-repair.lock.yml instead.
```

Manual dispatch pins the latest attempt once. Old successful runs, foreign
repositories, unsupported branches, and PR heads that have moved are rejected.
Closing a repair issue does not reset the automatic-attempt budget.

## Maintenance

Edit the `.md` workflow sources, not their generated `.lock.yml` files. Compile
with `github/gh-aw` **v0.92.0**:

```sh
gh aw compile termux-watch-repair termux-build-repair --no-check-update
python3 -B .github/scripts/test_termux_repair.py
```

`termux-repair-tests.yml` checks the policy tests and recompiles the workflows
with a checksum-pinned compiler. A changed lock file fails CI. Review permission,
secret, action, and sandbox changes before approving a compiler upgrade.

To inspect failure selection locally without creating an issue or invoking an
agent, set `GITHUB_REPOSITORY`, `REPAIR_MODE`, `REPAIR_RUN_ID`, and a temporary
`GITHUB_OUTPUT`, then run `.github/scripts/termux-repair.py plan`.

References: [GitHub Agentic Workflows authentication](https://github.github.com/gh-aw/reference/auth/),
[safe pull-request outputs](https://github.github.com/gh-aw/reference/safe-outputs-pull-requests/).
