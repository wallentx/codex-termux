#!/usr/bin/env bash

set -euo pipefail

source_branch="${SOURCE_BRANCH:-${REQUESTED_SOURCE_BRANCH:-${GITHUB_REF_NAME}}}"
source_sha="${SOURCE_SHA:-${REQUESTED_SOURCE_SHA:-}}"
if [[ -z "${source_sha}" ]]; then
  if [[ "${GITHUB_EVENT_NAME:-}" == "push" && "${source_branch}" == "${GITHUB_REF_NAME:-}" ]]; then
    source_sha="${GITHUB_SHA}"
  else
    source_sha="$(git rev-parse "origin/${source_branch}")"
  fi
fi

if [[ -z "${DESTINATION_BRANCH:-}" ]]; then
  echo "DESTINATION_BRANCH is required." >&2
  exit 1
fi

release_only_checkpoint_paths() {
  printf '%s\n' \
    scripts/termux-create-checkpoint-pr.sh \
    scripts/termux-download-release-artifact.sh \
    scripts/termux-find-release-pr.sh
}

short_sha="${source_sha:0:12}"
source_slug="${source_branch//\//_}"
dest_slug="${DESTINATION_BRANCH//\//_}"
checkpoint_branch="checkpoint/${dest_slug}_from_${source_slug}_${short_sha}"
pr_title="checkpoint: into ${DESTINATION_BRANCH} from ${source_branch} @ ${short_sha}"
merge_conflicted=false
conflict_summary=""

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
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "pr_url=${existing_url}" >> "${GITHUB_OUTPUT}"
  fi
  exit 0
fi

git checkout -B "${checkpoint_branch}" "origin/${DESTINATION_BRANCH}"

if ! git merge --no-ff --no-edit "${source_sha}"; then
  is_release_only_checkpoint_path() {
    case "$1" in
      .github/workflows/rust-release.yml|\
      .github/workflows/shell-tool-mcp.yml|\
      .github/workflows/termux-release-checkpoint.yml|\
      .github/workflows/termux-release-deploy.yml|\
      .github/workflows/termux-release-promote.yml|\
      .github/termux-release.json|\
      scripts/termux-create-checkpoint-pr.sh|\
      scripts/termux-download-release-artifact.sh|\
      scripts/termux-find-release-pr.sh)
        return 0
        ;;
      *)
        return 1
        ;;
    esac
  }

  mapfile -t conflicted_paths < <(git diff --name-only --diff-filter=U)
  for conflicted_path in "${conflicted_paths[@]}"; do
    if is_release_only_checkpoint_path "${conflicted_path}"; then
      echo "Auto-resolving release-only checkpoint conflict in ${conflicted_path} by keeping ${DESTINATION_BRANCH}."
      if git cat-file -e "HEAD:${conflicted_path}" 2>/dev/null; then
        git checkout --ours -- "${conflicted_path}"
        git add "${conflicted_path}"
      else
        git rm -f --ignore-unmatch "${conflicted_path}"
      fi
    fi
  done

  mapfile -t remaining_conflicts < <(git diff --name-only --diff-filter=U)
  if [[ "${#remaining_conflicts[@]}" -eq 1 && "${remaining_conflicts[0]}" == "codex-rs/Cargo.toml" ]]; then
    echo "Auto-resolving recurring codex-rs/Cargo.toml checkpoint conflict by keeping ${source_branch}."
    git checkout --theirs -- codex-rs/Cargo.toml
    git add codex-rs/Cargo.toml
  fi

  mapfile -t remaining_conflicts < <(git diff --name-only --diff-filter=U)
  if [[ "${#remaining_conflicts[@]}" -eq 0 ]]; then
    git commit --no-edit
  else
    merge_conflicted=true
    conflict_summary="$(
      printf '%s\n' "${remaining_conflicts[@]}" | sed 's/^/- `&`/'
    )"
    echo "Automatic checkpoint merge failed; creating a manual-resolution PR instead." >&2
    if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
      git merge --abort
    fi
    git checkout -B "${checkpoint_branch}" "${source_sha}"
  fi
fi

if git cat-file -e "origin/${DESTINATION_BRANCH}:.github" 2>/dev/null; then
  git restore --source="origin/${DESTINATION_BRANCH}" --staged --worktree -- .github
  mapfile -t added_github_paths < <(
    git diff --name-only --diff-filter=A "origin/${DESTINATION_BRANCH}" -- .github
  )
  if ((${#added_github_paths[@]})); then
    git rm -f --ignore-unmatch -- "${added_github_paths[@]}"
  fi
else
  git rm -r --ignore-unmatch .github
fi

while IFS= read -r release_only_path; do
  if git cat-file -e "origin/${DESTINATION_BRANCH}:${release_only_path}" 2>/dev/null; then
    git restore --source="origin/${DESTINATION_BRANCH}" --staged --worktree -- "${release_only_path}"
  else
    git rm -f --ignore-unmatch -- "${release_only_path}"
  fi
done < <(release_only_checkpoint_paths)

if ! git diff --quiet || ! git diff --cached --quiet; then
  git add -A .github
  while IFS= read -r release_only_path; do
    git add -A -- "${release_only_path}"
  done < <(release_only_checkpoint_paths)
  if [[ "${merge_conflicted}" == "true" ]]; then
    git commit -m "checkpoint: prepare ${source_branch} for ${DESTINATION_BRANCH}"
  else
    git commit --amend --no-edit
  fi
fi

if git diff --quiet "origin/${DESTINATION_BRANCH}" HEAD; then
  echo "Checkpoint merge produced no destination changes after release-only files were restored."
  exit 0
fi

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
  if [[ "${merge_conflicted}" == "true" ]]; then
    echo
    echo "## Merge conflicts"
    echo
    echo "GitHub Actions could not create the checkpoint merge commit automatically, so this PR was created from the source branch state for manual conflict resolution."
    echo
    echo "Conflicted paths from the failed merge attempt:"
    if [[ -n "${conflict_summary}" ]]; then
      printf '%s\n' "${conflict_summary}"
    else
      echo "- Conflict details unavailable"
    fi
  fi
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

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "pr_url=${pr_url}" >> "${GITHUB_OUTPUT}"
fi
