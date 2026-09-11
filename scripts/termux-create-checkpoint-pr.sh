#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/termux-release-paths.sh
source "${script_dir}/termux-release-paths.sh"

source_branch="${SOURCE_BRANCH:-${REQUESTED_SOURCE_BRANCH:-${GITHUB_REF_NAME}}}"
source_sha="${SOURCE_SHA:-${REQUESTED_SOURCE_SHA:-}}"
if [[ -z "${source_sha}" ]]; then
  if [[ "${GITHUB_EVENT_NAME:-}" == "push" && "${source_branch}" == "${GITHUB_REF_NAME:-}" ]]; then
    source_sha="${GITHUB_SHA}"
  else
    source_sha="$(git rev-parse "origin/${source_branch}")"
  fi
fi
source_sha="$(git rev-parse --verify "${source_sha}^{commit}")"

if [[ -z "${DESTINATION_BRANCH:-}" ]]; then
  echo "DESTINATION_BRANCH is required." >&2
  exit 1
fi
enable_checkpoint_automerge() {
  local pr_url="$1"

  local pr_info
  pr_info="$(
    gh pr view "${pr_url}" \
      --repo "${GITHUB_REPOSITORY}" \
      --json headRefOid,state,url,body,isDraft,mergeable,mergeStateStatus
  )"

  local pr_state
  pr_state="$(jq -r '.state' <<< "${pr_info}")"
  if [[ "${pr_state}" != "OPEN" ]]; then
    echo "Skipping checkpoint auto-merge for ${pr_url}; PR is ${pr_state}."
    return 0
  fi

  if jq -e '.isDraft == true or .mergeable == "CONFLICTING" or .mergeStateStatus == "DIRTY"
      or ((.body // "") | contains("## Merge conflicts"))' <<< "${pr_info}" >/dev/null; then
    gh pr merge "${pr_url}" --repo "${GITHUB_REPOSITORY}" --disable-auto
    echo "Checkpoint ${pr_url} needs review; auto-merge is disabled." >&2
    return 1
  fi

  local pr_head_sha
  pr_head_sha="$(jq -r '.headRefOid' <<< "${pr_info}")"

  echo "Enabling merge-commit auto-merge for checkpoint PR ${pr_url}."
  gh pr merge "${pr_url}" \
    --repo "${GITHUB_REPOSITORY}" \
    --merge \
    --auto \
    --delete-branch \
    --match-head-commit "${pr_head_sha}"
}

short_sha="${source_sha:0:12}"
source_slug="${source_branch//\//_}"
dest_slug="${DESTINATION_BRANCH//\//_}"
checkpoint_branch="checkpoint/${dest_slug}_from_${source_slug}_${short_sha}"
pr_title="checkpoint: into ${DESTINATION_BRANCH} from ${source_branch} @ ${short_sha}"

existing_pr="$(
  gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --head "${checkpoint_branch}" \
    --state all \
    --json number,state,mergedAt,url \
    --jq '[.[] | select(.state == "OPEN" or .mergedAt != null)] | .[0] // empty'
)"
if [[ -n "${existing_pr}" ]]; then
  existing_url="$(jq -r '.url' <<< "${existing_pr}")"
  existing_state="$(jq -r '.state' <<< "${existing_pr}")"
  echo "Checkpoint PR already exists for ${checkpoint_branch}: ${existing_url} (${existing_state})."
  if [[ "${existing_state}" == "OPEN" ]]; then
    enable_checkpoint_automerge "${existing_url}"
  fi
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "pr_url=${existing_url}" >> "${GITHUB_OUTPUT}"
  fi
  exit 0
fi

destination_sha="$(git rev-parse "origin/${DESTINATION_BRANCH}^{commit}")"
if ! checkpoint_tree="$(bash "${script_dir}/termux-checkpoint-tree.sh" \
  "${source_sha}" "${destination_sha}" "${DESTINATION_BRANCH}")"; then
  exit 1
fi
if git diff --quiet "${destination_sha}" "${checkpoint_tree}"; then
  echo "Tested code is already checkpointed; no destination changes."
  exit 0
fi

git checkout -B "${checkpoint_branch}" "${destination_sha}"
git status --short --branch
git var GIT_AUTHOR_IDENT
checkpoint_commit="$(git commit-tree "${checkpoint_tree}" \
  -p "${destination_sha}" -p "${source_sha}" \
  -m "checkpoint: tested ${source_branch} into ${DESTINATION_BRANCH}")"
git merge --ff-only "${checkpoint_commit}"

git push --force-with-lease origin "${checkpoint_branch}"

remaining="$(
  git log --first-parent --pretty=format:%H "${source_sha}..origin/${source_branch}" | wc -w
)"

body_path="${RUNNER_TEMP}/termux-checkpoint-pr.md"
{
  echo "## Termux release checkpoint"
  echo
  echo "- Source branch: \`${source_branch}\`"
  echo "- Source hash: \`${source_sha}\`"
  echo "- Destination branch: \`${DESTINATION_BRANCH}\`"
  echo "- Remaining first-parent commits on source: ${remaining}"
  echo
  echo "This PR carries release-train conflict fixes and follow-up changes back into the reusable Termux patch branch."
  echo "The merge uses the release metadata's recorded patch-source snapshot, preserving independent target changes made since the release was prepared. No conflicting code was discarded."
  echo
  echo "Release-only workflow files and metadata under \`.github\` were restored to the destination branch versions before opening this PR."
} > "${body_path}"

pr_url="$(
  gh pr create \
    --repo "${GITHUB_REPOSITORY}" \
    --base "${DESTINATION_BRANCH}" \
    --head "${checkpoint_branch}" \
    --title "${pr_title}" \
    --body-file "${body_path}"
)"
gh pr edit "${pr_url}" --repo "${GITHUB_REPOSITORY}" --add-reviewer "${REVIEWER}" || true
gh label create checkpoint --repo "${GITHUB_REPOSITORY}" --color c5def5 --description "Checkpoint merge" --force
gh label create termux-release --repo "${GITHUB_REPOSITORY}" --color 0e8a16 --description "Termux release automation" --force
gh pr edit "${pr_url}" --repo "${GITHUB_REPOSITORY}" --add-label "checkpoint" --add-label "termux-release"
enable_checkpoint_automerge "${pr_url}"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "pr_url=${pr_url}" >> "${GITHUB_OUTPUT}"
fi
