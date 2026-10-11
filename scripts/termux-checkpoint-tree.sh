#!/usr/bin/env bash

# Produce the checkpoint tree without changing a branch, worktree, or real index.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/termux-release-paths.sh
source "${script_dir}/termux-release-paths.sh"

source_sha="$(git rev-parse --verify "${1:?source commit required}^{commit}")"
destination_sha="$(git rev-parse --verify "${2:?destination commit required}^{commit}")"
destination_branch="${3:?destination branch required}"
metadata="$(git show "${source_sha}:.github/termux-release.json")"
patch_source_sha="$(jq -er '.patch_source_sha | select(type == "string")' <<< "${metadata}")"
patch_branch="$(jq -er '.patch_branch | select(type == "string")' <<< "${metadata}")"

if [[ "${patch_branch}" != "${destination_branch}" ]]; then
  echo "Checkpoint destination ${destination_branch} differs from recorded patch branch ${patch_branch}." >&2
  exit 1
fi
if [[ ! "${patch_source_sha}" =~ ^[0-9a-f]{40}$ && ! "${patch_source_sha}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Release metadata has an invalid patch_source_sha." >&2
  exit 1
fi
git rev-parse --verify "${patch_source_sha}^{commit}" >/dev/null
if ! git merge-base --is-ancestor "${patch_source_sha}" "${destination_sha}"; then
  echo "Recorded patch source is not an ancestor of ${destination_branch}; refusing to replace unrelated code." >&2
  exit 1
fi

scratch="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:?TMPDIR or RUNNER_TEMP is required}}/termux-checkpoint-tree.XXXXXX")"
trap 'rm -rf "${scratch}"' EXIT

code_tree() {
  local ref="$1"
  local path
  local index="${scratch}/index"
  GIT_INDEX_FILE="${index}" git read-tree "${ref}" || return 1
  # Keep automation identical to the destination on BOTH sides of the merge.
  # It then cannot generate conflicts or leak into the compatibility branch.
  for path in .github "${TERMUX_RELEASE_BRANCH_SCRIPT_PATHS[@]}"; do
    if git cat-file -e "${destination_sha}:${path}" 2>/dev/null \
      || [[ -n "$(GIT_INDEX_FILE="${index}" git ls-files -- "${path}")" ]]; then
      GIT_INDEX_FILE="${index}" git restore --source="${destination_sha}" --staged -- "${path}" || return 1
    fi
  done
  GIT_INDEX_FILE="${index}" git write-tree
}

base_tree="$(code_tree "${patch_source_sha}")"
source_tree="$(code_tree "${source_sha}")"
echo "Checkpointing tested code against recorded target snapshot ${patch_source_sha}." >&2

# Release PRs are squash-merged. Their ordinary merge base can be much older
# than the target snapshot actually used to build and test the release.
# An explicit base preserves subsequent independent target edits and reports
# real concurrent edits instead of repeatedly conflicting on old upstream code.
if ! merged_tree="$(git merge-tree --write-tree --name-only --no-messages \
  --merge-base="${base_tree}" "${destination_sha}" "${source_tree}")"; then
  echo "Checkpoint has concurrent code changes requiring review; no checkpoint was published." >&2
  printf '%s\n' "${merged_tree}" >&2
  exit 1
fi
if [[ "$(git cat-file -t "${merged_tree}")" != tree ]]; then
  echo "Checkpoint merge did not produce a tree." >&2
  exit 1
fi
printf '%s\n' "${merged_tree}"
