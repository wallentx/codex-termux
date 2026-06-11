#!/usr/bin/env bash

# Sourceable path lists for files owned by the Termux release automation.

set -euo pipefail

readonly -a TERMUX_RELEASE_WORKFLOW_PATHS=(
  .github/workflows/rust-release.yml
  .github/workflows/shell-tool-mcp.yml
  .github/workflows/termux-release-checkpoint.yml
  .github/workflows/termux-release-deploy.yml
  .github/workflows/termux-release-promote.yml
)

readonly -a TERMUX_RELEASE_BRANCH_SCRIPT_PATHS=(
  scripts/termux-configure-git.sh
  scripts/termux-create-checkpoint-pr.sh
  scripts/termux-create-or-update-mirrored-release.sh
  scripts/termux-download-release-artifact.sh
  scripts/termux-find-release-pr.sh
  scripts/termux-read-release-metadata.sh
  scripts/termux-release-asset-state.sh
  scripts/termux-release-paths.sh
  scripts/termux-resolve-release-ref.sh
  scripts/termux-validate-gh-env.sh
)

readonly -a TERMUX_RELEASE_AUTOMATION_PATHS=(
  "${TERMUX_RELEASE_WORKFLOW_PATHS[@]}"
  "${TERMUX_RELEASE_BRANCH_SCRIPT_PATHS[@]}"
)

readonly -a TERMUX_CHECKPOINT_RELEASE_ONLY_PATHS=(
  "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"
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
