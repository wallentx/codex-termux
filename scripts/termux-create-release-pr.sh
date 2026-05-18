#!/usr/bin/env bash

set -euo pipefail

# Required env: GITHUB_REPOSITORY, UPSTREAM_REPO, UPSTREAM_TAG,
# UPSTREAM_NAME, UPSTREAM_HTML_URL, UPSTREAM_PRERELEASE, UPSTREAM_TARGET,
# UPSTREAM_ID, RELEASE_TRAIN, RELEASE_BRANCH, WORK_BRANCH, TERMUX_TAG,
# PATCH_BRANCH, REVIEWER, RUNNER_TEMP, GITHUB_OUTPUT. GH_TOKEN must authenticate gh.

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${UPSTREAM_REPO:?UPSTREAM_REPO is required}"
: "${UPSTREAM_TAG:?UPSTREAM_TAG is required}"
: "${UPSTREAM_NAME?UPSTREAM_NAME is required}"
: "${UPSTREAM_HTML_URL:?UPSTREAM_HTML_URL is required}"
: "${UPSTREAM_PRERELEASE:?UPSTREAM_PRERELEASE is required}"
: "${UPSTREAM_TARGET?UPSTREAM_TARGET is required}"
: "${UPSTREAM_ID?UPSTREAM_ID is required}"
: "${RELEASE_TRAIN:?RELEASE_TRAIN is required}"
: "${RELEASE_BRANCH:?RELEASE_BRANCH is required}"
: "${WORK_BRANCH:?WORK_BRANCH is required}"
: "${TERMUX_TAG:?TERMUX_TAG is required}"
: "${PATCH_BRANCH:?PATCH_BRANCH is required}"
: "${REVIEWER:?REVIEWER is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
automation_root="$(cd -- "${script_dir}/.." && pwd)"
# shellcheck source=scripts/termux-release-paths.sh
source "${script_dir}/termux-release-paths.sh"

pr_title="Termux ${UPSTREAM_TAG}"
integration_conflicted=false
conflict_summary=""
conflict_context=""
fallback_ref=""

seed_dir="${RUNNER_TEMP}/termux-release-seed"

capture_seeded_release_files() {
  local seeded_path

  for seeded_path in "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"; do
    mkdir -p "${seed_dir}/$(dirname "${seeded_path}")"
    cp "${automation_root}/${seeded_path}" "${seed_dir}/${seeded_path}"
  done
}

seed_release_branch_workflows() {
  # Release branches start from upstream tags, so keep the Termux-owned
  # CI/deployment workflows and their helper scripts authoritative from
  # this automation branch.
  local seeded_path

  for seeded_path in "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"; do
    mkdir -p "$(dirname "${seeded_path}")"
    cp "${seed_dir}/${seeded_path}" "${seeded_path}"
    case "${seeded_path}" in
      scripts/*.sh)
        chmod +x "${seeded_path}"
        ;;
    esac
  done
}

git_add_seeded_release_paths() {
  git add -- "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"
}

git_diff_without_seeded_release_paths() {
  local range="$1"
  local seeded_path
  local pathspecs=(-- .)

  for seeded_path in "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"; do
    pathspecs+=(":(exclude)${seeded_path}")
  done

  git diff --binary "${range}" "${pathspecs[@]}"
}

resolve_seeded_release_workflow_conflicts() {
  local conflicted_path
  local resolved_any=false

  mapfile -t conflicted_paths < <(git diff --name-only --diff-filter=U)
  for conflicted_path in "${conflicted_paths[@]}"; do
    if termux_is_release_automation_path "${conflicted_path}"; then
      resolved_any=true
    fi
  done

  if [[ "${resolved_any}" != "true" ]]; then
    return 0
  fi

  echo "Auto-resolving Termux-owned release workflow conflicts from the automation branch."
  seed_release_branch_workflows
  git_add_seeded_release_paths
}

resolve_workspace_version_conflict() {
  local upstream_version

  upstream_version="$(workspace_version_from_ref "refs/tags/${UPSTREAM_TAG}")"
  if [[ -z "${upstream_version}" ]]; then
    echo "Unable to read workspace package version from refs/tags/${UPSTREAM_TAG}" >&2
    return 0
  fi

  if UPSTREAM_WORKSPACE_VERSION="${upstream_version}" perl -0pi -e '
    my $version = $ENV{"UPSTREAM_WORKSPACE_VERSION"};
    s/<<<<<<<[^\n]*\n(version = ")[^"]+("\n)=======\n\1[^"]+\2>>>>>>>[^\n]*(\n)/$1$version$2$3/s
      or exit 1;
  ' codex-rs/Cargo.toml; then
    git add codex-rs/Cargo.toml
  else
    echo "codex-rs/Cargo.toml has conflicts beyond the simple workspace version bump; leaving it for the fallback PR."
    git checkout -m -- codex-rs/Cargo.toml
  fi
}

resolve_known_release_train_conflicts() {
  local conflicted_path

  resolve_seeded_release_workflow_conflicts

  mapfile -t conflicted_paths < <(git diff --name-only --diff-filter=U)
  for conflicted_path in "${conflicted_paths[@]}"; do
    case "${conflicted_path}" in
      .github/workflows/*)
        echo "Auto-resolving upstream workflow conflict in ${conflicted_path} by keeping ${RELEASE_BRANCH}."
        git checkout --ours -- "${conflicted_path}"
        git add "${conflicted_path}"
        ;;
    esac
  done

  mapfile -t conflicted_paths < <(git diff --name-only --diff-filter=U)
  for conflicted_path in "${conflicted_paths[@]}"; do
    case "${conflicted_path}" in
      codex-rs/Cargo.toml)
        echo "Auto-resolving recurring workspace version conflict in codex-rs/Cargo.toml."
        resolve_workspace_version_conflict
        ;;
      codex-rs/app-server/tests/suite/v2/thread_resume.rs)
        echo "Auto-resolving upstream-only app-server test conflict in ${conflicted_path} by keeping refs/tags/${UPSTREAM_TAG}."
        git checkout --theirs -- "${conflicted_path}"
        git add "${conflicted_path}"
        ;;
    esac
  done
}

reset_for_fallback_checkout() {
  git reset --hard "origin/${RELEASE_BRANCH}"
  git clean -fd .github/workflows
}

workspace_version_from_ref() {
  local ref="$1"
  git show "${ref}:codex-rs/Cargo.toml" | awk '
    /^\[workspace\.package\]$/ { in_workspace_package = 1; next }
    /^\[/ { in_workspace_package = 0 }
    in_workspace_package && /^version = / {
      gsub(/^version = "/, "")
      gsub(/"$/, "")
      print
      exit
    }
  '
}

normalize_patch_branch_version() {
  local normalized_ref="$1"
  local upstream_version

  upstream_version="$(workspace_version_from_ref "refs/tags/${UPSTREAM_TAG}")"
  if [[ -z "${upstream_version}" ]]; then
    echo "Unable to read workspace package version from refs/tags/${UPSTREAM_TAG}" >&2
    exit 1
  fi

  git checkout -B "${normalized_ref}" "origin/${PATCH_BRANCH}"
  if [[ ! -f codex-rs/Cargo.toml ]]; then
    echo "codex-rs/Cargo.toml is missing from ${PATCH_BRANCH}" >&2
    exit 1
  fi

  UPSTREAM_WORKSPACE_VERSION="${upstream_version}" perl -0pi -e '
    my $version = $ENV{"UPSTREAM_WORKSPACE_VERSION"};
    s/(\[workspace\.package\]\n(?:(?!^\[).*\n)*?version = ")[^"]+(")/$1$version$2/m
      or die "workspace.package version not found\n";
  ' codex-rs/Cargo.toml

  if ! git diff --quiet -- codex-rs/Cargo.toml; then
    git add codex-rs/Cargo.toml
    git commit -m "Normalize Termux patch workspace version"
  fi
}

open_prs_cache_loaded=false
open_prs_cache="[]"
open_prs_json() {
  if [[ "${open_prs_cache_loaded}" != "true" ]]; then
    open_prs_cache="$(
      gh pr list \
        --repo "${GITHUB_REPOSITORY}" \
        --state open \
        --limit 200 \
        --json number,title,body,headRefName,baseRefName,url
    )"
    open_prs_cache_loaded=true
  fi
  printf '%s\n' "${open_prs_cache}"
}

title_prs_cache_loaded=false
title_prs_cache="[]"
title_prs_json() {
  if [[ "${title_prs_cache_loaded}" != "true" ]]; then
    title_prs_cache="$(
      gh pr list \
        --repo "${GITHUB_REPOSITORY}" \
        --state all \
        --search "\"${pr_title}\" in:title" \
        --limit 100 \
        --json number,title,state,isDraft,mergedAt,url
    )"
    title_prs_cache_loaded=true
  fi
  printf '%s\n' "${title_prs_cache}"
}

append_pr_summary() {
  local outcome="$1"
  local pr_url="${2:-}"
  local short_repo="${REPO:-${GITHUB_REPOSITORY##*/}}"
  local repo_url="${GH_REPO_URL:-https://github.com/${GITHUB_REPOSITORY}}"

  [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] || return 0

  {
    echo "## Termux release train PR"
    echo
    echo "- Repository: [${short_repo}](${repo_url})"
    if [[ -n "${GH_WORKFLOW_URL:-}" ]]; then
      echo "- Workflow run: ${GH_WORKFLOW_URL}"
    fi
    echo "- Outcome: ${outcome}"
    echo "- Selected upstream tag: \`${UPSTREAM_TAG}\`"
    echo "- Release kind: \`codex\`"
    echo "- Release train branch: \`${RELEASE_BRANCH}\`"
    echo "- Work branch: \`${WORK_BRANCH}\`"
    echo "- Conflict fallback/manual resolution: \`${integration_conflicted}\`"
    if [[ "${integration_conflicted}" == "true" ]]; then
      echo "- Conflict context: ${conflict_context}"
      echo "- Fallback ref: \`${fallback_ref}\`"
    fi
    if [[ -n "${pr_url}" ]]; then
      echo "- PR: ${pr_url}"
    fi
  } >> "${GITHUB_STEP_SUMMARY}"
}

release_branch_open_pr_blockers() {
  local release_branch="$1"
  local release_slug="${release_branch//\//_}"
  local open_prs

  open_prs="$(open_prs_json)"

  jq -r \
    --arg release_branch "${release_branch}" \
    --arg release_slug "${release_slug}" \
    '
      .[]
      | select(
          .baseRefName == $release_branch
          or .headRefName == $release_branch
          or (.headRefName | startswith("checkpoint/") and contains("_from_" + $release_slug + "_"))
          or ((.title // "") | contains($release_branch))
          or ((.body // "") | contains("Source branch: `" + $release_branch + "`"))
          or ((.body // "") | contains("Release train branch: `" + $release_branch + "`"))
        )
      | "- #\(.number) \(.title) (\(.url))"
    ' <<< "${open_prs}"
}

delete_existing_release_branch_if_safe() {
  local release_branch="$1"
  local blockers

  blockers="$(release_branch_open_pr_blockers "${release_branch}")"
  if [[ -n "${blockers}" ]]; then
    echo "Keeping ${release_branch}: open PRs/checkpoints still reference it."
    printf '%s\n' "${blockers}"
    return 0
  fi

  echo "Deleting existing ${release_branch} so ${UPSTREAM_TAG} can be rebuilt from the upstream tag."
  git push origin --delete "${release_branch}"
}

capture_seeded_release_files

existing_prs="$(title_prs_json)"
existing_merged_pr="$(
  jq -c --arg title "${pr_title}" '
    [.[] | select(.title == $title and (.state == "MERGED" or .mergedAt != null))]
    | sort_by(.number)
    | reverse
    | .[0] // empty
  ' <<< "${existing_prs}"
)"
if [[ -n "${existing_merged_pr}" ]]; then
  existing_pr_url="$(jq -r '.url' <<< "${existing_merged_pr}")"
  echo "A matching merged PR already exists for title '${pr_title}': ${existing_pr_url}."
  append_pr_summary "matching merged PR already exists" "${existing_pr_url}"
  exit 0
fi

existing_open_train_pr="$(
  jq -c --arg release_branch "${RELEASE_BRANCH}" '
    [
      .[]
      | select(.baseRefName == $release_branch)
      | select(
          ((.title // "") | startswith("Termux rust-v"))
          and ((.body // "") | contains("Release train branch: `" + $release_branch + "`"))
        )
    ]
    | sort_by(.number)
    | reverse
    | .[0] // empty
  ' <<< "$(open_prs_json)"
)"
existing_open_train_pr_url=""
if [[ -n "${existing_open_train_pr}" ]]; then
  existing_open_train_pr_url="$(jq -r '.url' <<< "${existing_open_train_pr}")"
  existing_open_train_pr_head="$(jq -r '.headRefName' <<< "${existing_open_train_pr}")"
  if [[ "${existing_open_train_pr_head}" != "${WORK_BRANCH}" ]]; then
    echo "Open release train PR uses ${existing_open_train_pr_head}; updating that branch instead of ${WORK_BRANCH}."
    WORK_BRANCH="${existing_open_train_pr_head}"
  fi
fi

release_branch_exists=false
if git ls-remote --exit-code --heads origin "${RELEASE_BRANCH}" >/dev/null 2>&1; then
  release_branch_exists=true
fi

if [[ "${release_branch_exists}" == true ]]; then
  delete_existing_release_branch_if_safe "${RELEASE_BRANCH}"
  if ! git ls-remote --exit-code --heads origin "${RELEASE_BRANCH}" >/dev/null 2>&1; then
    release_branch_exists=false
  fi
fi

if [[ "${release_branch_exists}" == false ]]; then
  git checkout -B "${RELEASE_BRANCH}" "refs/tags/${UPSTREAM_TAG}"
  seed_release_branch_workflows
  git_add_seeded_release_paths
  if ! git diff --cached --quiet; then
    git commit -m "Seed Termux release automation"
  fi
  git push origin "${RELEASE_BRANCH}"
fi

git fetch origin "${RELEASE_BRANCH}"
work_branch_exists=false
if git ls-remote --exit-code --heads origin "${WORK_BRANCH}" >/dev/null 2>&1; then
  work_branch_exists=true
  git fetch origin "${WORK_BRANCH}"
fi

if [[ "${work_branch_exists}" == true ]]; then
  git checkout -B "${WORK_BRANCH}" "origin/${WORK_BRANCH}"
else
  git checkout -B "${WORK_BRANCH}" "origin/${RELEASE_BRANCH}"
fi

if [[ "${release_branch_exists}" == false ]]; then
  normalized_patch_ref="termux-patch-normalized/${UPSTREAM_TAG}"
  normalize_patch_branch_version "${normalized_patch_ref}"
  git checkout -B "${WORK_BRANCH}" "origin/${RELEASE_BRANCH}"
  seed_release_branch_workflows

  echo "Creating Termux patch from refs/tags/${UPSTREAM_TAG} to ${normalized_patch_ref}."
  git_diff_without_seeded_release_paths "refs/tags/${UPSTREAM_TAG}..${normalized_patch_ref}" > "${RUNNER_TEMP}/termux.patch"
  if [[ -s "${RUNNER_TEMP}/termux.patch" ]]; then
    if ! git apply --3way "${RUNNER_TEMP}/termux.patch"; then
      integration_conflicted=true
      conflict_context="Applying the Termux patch branch onto the upstream tag"
      fallback_ref="origin/${PATCH_BRANCH}"
      conflict_summary="$(
        git diff --name-only --diff-filter=U | awk '{ print "- `" $0 "`" }'
      )"
      echo "Applying the Termux patch branch conflicted; creating a manual-resolution PR instead." >&2
      reset_for_fallback_checkout
      git checkout -B "${WORK_BRANCH}" "${fallback_ref}"
    fi
  fi
else
  if ! git merge --no-ff --no-edit "refs/tags/${UPSTREAM_TAG}"; then
    resolve_known_release_train_conflicts
    remaining_conflicts="$(git diff --name-only --diff-filter=U)"
    if [[ -z "${remaining_conflicts}" ]]; then
      git commit --no-edit
    else
      integration_conflicted=true
      conflict_context="Merging the upstream tag into the existing release train branch"
      fallback_ref="refs/tags/${UPSTREAM_TAG}"
      conflict_summary="$(
        printf '%s\n' "${remaining_conflicts}" | awk '{ print "- `" $0 "`" }'
      )"
      echo "Merging the upstream tag conflicted; creating a manual-resolution PR instead." >&2
      if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
        git merge --abort
      fi
      reset_for_fallback_checkout
      git checkout -B "${WORK_BRANCH}" "${fallback_ref}"
    fi
  fi
fi
seed_release_branch_workflows

mkdir -p .github
jq -n \
  --arg upstream_repo "${UPSTREAM_REPO}" \
  --arg upstream_tag "${UPSTREAM_TAG}" \
  --arg upstream_name "${UPSTREAM_NAME}" \
  --arg upstream_html_url "${UPSTREAM_HTML_URL}" \
  --arg upstream_target "${UPSTREAM_TARGET}" \
  --arg upstream_release_id "${UPSTREAM_ID}" \
  --arg release_train "${RELEASE_TRAIN}" \
  --arg release_branch "${RELEASE_BRANCH}" \
  --arg work_branch "${WORK_BRANCH}" \
  --arg patch_branch "${PATCH_BRANCH}" \
  --arg patch_source_sha "$(git rev-parse "origin/${PATCH_BRANCH}")" \
  --arg termux_tag "${TERMUX_TAG}" \
  --argjson upstream_prerelease "${UPSTREAM_PRERELEASE}" \
  '{
    upstream_repo: $upstream_repo,
    upstream_tag: $upstream_tag,
    upstream_name: $upstream_name,
    upstream_html_url: $upstream_html_url,
    upstream_target: $upstream_target,
    upstream_release_id: $upstream_release_id,
    upstream_prerelease: $upstream_prerelease,
    release_train: $release_train,
    release_branch: $release_branch,
    work_branch: $work_branch,
    patch_branch: $patch_branch,
    patch_source_sha: $patch_source_sha,
    termux_tag: $termux_tag
  }' > .github/termux-release.json

git add -A
if git diff --cached --quiet; then
  echo "No changes to propose for ${UPSTREAM_TAG}."
  append_pr_summary "no changes to propose"
  exit 0
fi

git commit -m "Prepare Termux ${UPSTREAM_TAG}"
git push --force-with-lease origin "${WORK_BRANCH}"

body_path="${RUNNER_TEMP}/termux-release-pr.md"
{
  echo "## Termux release train"
  echo
  echo "- Upstream release: ${UPSTREAM_HTML_URL}"
  echo "- Upstream tag: \`${UPSTREAM_TAG}\`"
  echo "- Termux release tag: \`${TERMUX_TAG}\`"
  echo "- Release train branch: \`${RELEASE_BRANCH}\`"
  echo "- Patch source: \`${PATCH_BRANCH}\`"
  echo
  echo "Merging this PR is the manual approval gate. The release build workflow uploads the Android artifact to test; after merge, the deployment workflow attaches that exact artifact to \`${TERMUX_TAG}\` and opens the checkpoint PR."
  if [[ "${integration_conflicted}" == "true" ]]; then
    echo
    echo "## Merge conflicts"
    echo
    echo "${conflict_context} conflicted in GitHub Actions, so this PR was created from \`${fallback_ref}\` for manual resolution."
    echo
    echo "Conflicted paths from the failed integration attempt:"
    if [[ -n "${conflict_summary}" ]]; then
      printf '%s\n' "${conflict_summary}"
    else
      echo "- Conflict details unavailable"
    fi
  fi
  echo
  echo "## Upstream notes"
  echo
  printf '%s\n' "${UPSTREAM_BODY}"
} > "${body_path}"

if [[ -n "${existing_open_train_pr_url}" ]]; then
  pr_action="updated"
  pr_url="${existing_open_train_pr_url}"
  gh pr edit "${pr_url}" \
    --repo "${GITHUB_REPOSITORY}" \
    --title "${pr_title}" \
    --body-file "${body_path}"
else
  pr_action="created"
  pr_url="$(
    gh pr create \
      --repo "${GITHUB_REPOSITORY}" \
      --base "${RELEASE_BRANCH}" \
      --head "${WORK_BRANCH}" \
      --title "${pr_title}" \
      --body-file "${body_path}"
  )"
fi
gh pr edit "${pr_url}" --repo "${GITHUB_REPOSITORY}" --add-reviewer "${REVIEWER}" || true
gh label create termux-release --repo "${GITHUB_REPOSITORY}" --color 0e8a16 --description "Termux release automation" --force
gh label create release-train --repo "${GITHUB_REPOSITORY}" --color 1d76db --description "Release train PR" --force
gh pr edit "${pr_url}" --repo "${GITHUB_REPOSITORY}" --add-label "termux-release" --add-label "release-train"
echo "pr_url=${pr_url}" >> "$GITHUB_OUTPUT"
append_pr_summary "${pr_action} release train PR" "${pr_url}"
