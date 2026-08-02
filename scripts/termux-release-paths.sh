#!/usr/bin/env bash

# Sourceable path lists for files owned by the Termux release automation.

set -euo pipefail

readonly -a TERMUX_RELEASE_WORKFLOW_PATHS=(
  .github/workflows/rust-release.yml
  .github/workflows/shell-tool-mcp.yml
  .github/workflows/termux-artifact-smoke.yml
  .github/workflows/termux-release-checkpoint.yml
  .github/workflows/termux-release-deploy.yml
  .github/workflows/termux-release-promote.yml
)

readonly -a TERMUX_RELEASE_GITHUB_SCRIPT_PATHS=(
  .github/scripts/termux-download-codex-artifact.sh
)

readonly -a TERMUX_RELEASE_BRANCH_SCRIPT_PATHS=(
  scripts/termux-configure-git.sh
  scripts/termux-create-checkpoint-pr.sh
  scripts/termux-create-or-update-mirrored-release.sh
  scripts/termux-download-release-artifact.sh
  scripts/termux-find-release-pr.sh
  scripts/termux-read-release-metadata.sh
  scripts/termux-release-self-update.patch
  scripts/termux-release-asset-state.sh
  scripts/termux-release-paths.sh
  scripts/termux-resolve-release-ref.sh
  scripts/termux-validate-gh-env.sh
)

readonly -a TERMUX_RELEASE_CODE_PATHS=(
  codex-rs/cli/src/main.rs
  codex-rs/tui/src/lib.rs
  codex-rs/tui/src/termux_update.rs
  codex-rs/tui/src/update_action.rs
  codex-rs/tui/src/update_prompt.rs
  codex-rs/tui/src/update_versions.rs
  codex-rs/tui/src/updates.rs
)

# These upstream-owned files have repeatedly retained stale checkpoint-side
# content when a new release branch was merged into the release work branch.
readonly -a TERMUX_RELEASE_UPSTREAM_AUTHORITATIVE_PATHS=(
  .github/scripts/build-codex-package-archive.sh
  .github/scripts/publish_r2_release.py
  .github/scripts/run-argument-comment-lint-bazel.sh
  .github/workflows/python-runtime-build.yml
  .github/workflows/python-runtime-release.yml
  .github/workflows/r2-release.yml
  codex-rs/Cargo.toml
)

readonly -a TERMUX_RELEASE_MERGE_AUTHORITATIVE_PATHS=(
  "${TERMUX_RELEASE_CODE_PATHS[@]}"
  "${TERMUX_RELEASE_UPSTREAM_AUTHORITATIVE_PATHS[@]}"
)

readonly -a TERMUX_RELEASE_AUTOMATION_PATHS=(
  "${TERMUX_RELEASE_WORKFLOW_PATHS[@]}"
  "${TERMUX_RELEASE_GITHUB_SCRIPT_PATHS[@]}"
  "${TERMUX_RELEASE_BRANCH_SCRIPT_PATHS[@]}"
)

readonly -a TERMUX_CHECKPOINT_RELEASE_ONLY_PATHS=(
  "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"
  "${TERMUX_RELEASE_CODE_PATHS[@]}"
  .github/termux-release.json
)

termux_path_in_list() {
  local candidate="$1"
  shift
  local listed_path

  for listed_path in "$@"; do
    [[ "${candidate}" != "${listed_path}" ]] || return 0
  done
  return 1
}

termux_is_release_automation_path() {
  termux_path_in_list "$1" "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"
}

termux_is_checkpoint_release_only_path() {
  termux_path_in_list "$1" "${TERMUX_CHECKPOINT_RELEASE_ONLY_PATHS[@]}"
}
