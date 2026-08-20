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

if [[ -z "${DESTINATION_BRANCH:-}" ]]; then
  echo "DESTINATION_BRANCH is required." >&2
  exit 1
fi

release_only_checkpoint_paths() {
  printf '%s\n' "${TERMUX_RELEASE_BRANCH_SCRIPT_PATHS[@]}"
}

resolve_source_version_conflicts() {
  local path="$1"
  local resolved_path
  resolved_path="$(mktemp)"

  if ! awk '
    function normalize_versions(text) {
      gsub(/version = "[^"]+"/, "version = \"<version>\"", text)
      return text
    }

    BEGIN {
      in_block = 0
      side = ""
      ours = ""
      theirs = ""
      blocks = 0
    }

    /^<<<<<<< / {
      if (in_block) {
        exit 1
      }
      in_block = 1
      side = "ours"
      ours = ""
      theirs = ""
      blocks++
      next
    }

    /^=======$/ && in_block {
      side = "theirs"
      next
    }

    /^>>>>>>> / && in_block {
      if (normalize_versions(ours) != normalize_versions(theirs)) {
        exit 1
      }
      printf "%s", theirs
      in_block = 0
      side = ""
      next
    }

    {
      if (!in_block) {
        print
      } else if (side == "ours") {
        ours = ours $0 ORS
      } else if (side == "theirs") {
        theirs = theirs $0 ORS
      } else {
        exit 1
      }
    }

    END {
      if (in_block || blocks == 0) {
        exit 1
      }
    }
  ' "${path}" > "${resolved_path}"; then
    rm -f "${resolved_path}"
    return 1
  fi

  cp "${resolved_path}" "${path}"
  rm -f "${resolved_path}"
}

enable_checkpoint_automerge() {
  local pr_url="$1"

  local pr_info
  pr_info="$(
    gh pr view "${pr_url}" \
      --repo "${GITHUB_REPOSITORY}" \
      --json headRefOid,state,url
  )"

  local pr_state
  pr_state="$(jq -r '.state' <<< "${pr_info}")"
  if [[ "${pr_state}" != "OPEN" ]]; then
    echo "Skipping checkpoint auto-merge for ${pr_url}; PR is ${pr_state}."
    return 0
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
  if [[ "${existing_state}" == "OPEN" ]]; then
    enable_checkpoint_automerge "${existing_url}"
  fi
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "pr_url=${existing_url}" >> "${GITHUB_OUTPUT}"
  fi
  exit 0
fi

git checkout -B "${checkpoint_branch}" "origin/${DESTINATION_BRANCH}"

if ! git merge --no-ff --no-edit "${source_sha}"; then
  mapfile -t conflicted_paths < <(git diff --name-only --diff-filter=U)
  for conflicted_path in "${conflicted_paths[@]}"; do
    if termux_is_checkpoint_release_only_path "${conflicted_path}"; then
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
  if ((${#remaining_conflicts[@]})); then
    cargo_version_conflicts=true
    for remaining_conflict in "${remaining_conflicts[@]}"; do
      case "${remaining_conflict}" in
        codex-rs/Cargo.toml|codex-rs/Cargo.lock)
          ;;
        *)
          cargo_version_conflicts=false
          ;;
      esac
    done

    if [[ "${cargo_version_conflicts}" == "true" ]]; then
      for remaining_conflict in "${remaining_conflicts[@]}"; do
        if ! resolve_source_version_conflicts "${remaining_conflict}"; then
          cargo_version_conflicts=false
          break
        fi
      done

      if [[ "${cargo_version_conflicts}" == "true" ]]; then
        echo "Auto-resolving recurring Cargo version checkpoint conflicts by keeping ${source_branch} versions."
        git add -- "${remaining_conflicts[@]}"
      fi
    fi
  fi

  mapfile -t remaining_conflicts < <(git diff --name-only --diff-filter=U)
  if [[ "${#remaining_conflicts[@]}" -eq 0 ]]; then
    git commit --no-edit
  else
    merge_conflicted=true
    conflict_summary="$(
      printf '%s\n' "${remaining_conflicts[@]}" | awk '{ print "- `" $0 "`" }'
    )"
    echo "Automatic checkpoint merge needs manual resolution; keeping destination versions for conflicted paths." >&2
    for remaining_conflict in "${remaining_conflicts[@]}"; do
      if git cat-file -e "HEAD:${remaining_conflict}" 2>/dev/null; then
        git checkout --ours -- "${remaining_conflict}"
        git add "${remaining_conflict}"
      else
        git rm -f --ignore-unmatch "${remaining_conflict}"
      fi
    done
    git commit --no-edit
  fi
fi

if git cat-file -e "origin/${DESTINATION_BRANCH}:.github" 2>/dev/null; then
  git checkout "origin/${DESTINATION_BRANCH}" -- .github
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
    git checkout "origin/${DESTINATION_BRANCH}" -- "${release_only_path}"
  else
    git rm -f --ignore-unmatch -- "${release_only_path}"
  fi
done < <(release_only_checkpoint_paths)

if ! git diff --quiet || ! git diff --cached --quiet; then
  if [[ -e .github || -L .github ]] || git ls-files --error-unmatch -- .github >/dev/null 2>&1; then
    git add -A .github
  fi
  while IFS= read -r release_only_path; do
    if [[ -e "${release_only_path}" || -L "${release_only_path}" ]] || git ls-files --error-unmatch -- "${release_only_path}" >/dev/null 2>&1; then
      git add -A -- "${release_only_path}"
    fi
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
    echo "GitHub Actions kept the destination branch versions of the conflicted paths so the PR remains a focused, mergeable checkpoint instead of exposing the entire source branch as a replacement tree."
    echo
    echo "Destination versions of these paths are retained in the checkpoint. Any source-side changes that are still needed can be carried forward separately."
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
enable_checkpoint_automerge "${pr_url}"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "pr_url=${pr_url}" >> "${GITHUB_OUTPUT}"
fi
