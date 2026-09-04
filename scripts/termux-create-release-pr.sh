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

apply_seeded_release_code_patches() {
  local patch_path="${TERMUX_RELEASE_CODE_PATCH:-${seed_dir}/scripts/termux-release-self-update.patch}"

  if git apply --reverse --check "${patch_path}" >/dev/null 2>&1; then
    echo "Termux release code patch is already applied."
    return 0
  fi

  git apply --3way "${patch_path}"
}

git_add_seeded_release_paths() {
  git add -- "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"
  git add -- "${TERMUX_RELEASE_CODE_PATHS[@]}"
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

normalize_workspace_version_to_upstream_tag() {
  local upstream_version

  upstream_version="$(workspace_version_from_ref "refs/tags/${UPSTREAM_TAG}")"
  if [[ -z "${upstream_version}" ]]; then
    echo "Unable to read workspace package version from refs/tags/${UPSTREAM_TAG}" >&2
    exit 1
  fi
  if [[ ! -f codex-rs/Cargo.toml ]]; then
    echo "codex-rs/Cargo.toml is missing from ${WORK_BRANCH}" >&2
    exit 1
  fi

  UPSTREAM_WORKSPACE_VERSION="${upstream_version}" perl -0pi -e '
    my $version = $ENV{"UPSTREAM_WORKSPACE_VERSION"};
    s/(\[workspace\.package\]\n(?:(?!^\[).*\n)*?version = ")[^"]+(")/$1$version$2/m
      or die "workspace.package version not found\n";
  ' codex-rs/Cargo.toml
}

restore_release_workspace_manifest() {
  local release_ref="origin/${RELEASE_BRANCH}"
  local manifest="codex-rs/Cargo.toml"

  git restore --source="${release_ref}" --staged --worktree -- "${manifest}"

  if git cat-file -e "HEAD:codex-rs/utils/file-lock/Cargo.toml" 2>/dev/null; then
    if ! grep -q '^[[:space:]]*"utils/file-lock",[[:space:]]*$' "${manifest}"; then
      perl -0pi -e '
        s{(^\s*"utils/elapsed",\n)}{$1    "utils/file-lock",\n}m
          || s{(members = \[\n)}{$1    "utils/file-lock",\n}
          || die "workspace members array not found\n";
      ' "${manifest}"
    fi
    if ! grep -q '^codex-utils-file-lock[[:space:]]*=' "${manifest}"; then
      perl -0pi -e '
        s{(^codex-utils-elapsed = .*\n)}{$1codex-utils-file-lock = { path = "utils/file-lock" }\n}m
          || s{(\[workspace\.dependencies\]\n)}{$1codex-utils-file-lock = { path = "utils/file-lock" }\n}
          || die "workspace dependencies section not found\n";
      ' "${manifest}"
    fi
  fi

  git add -- "${manifest}"
}

resolve_union_merge_paths() {
  local path
  local merge_dir

  for path in "${TERMUX_RELEASE_UNION_MERGE_PATHS[@]}"; do
    if [[ -z "$(git ls-files --unmerged -- "${path}")" ]]; then
      continue
    fi

    merge_dir="$(mktemp -d "${RUNNER_TEMP}/termux-release-union.XXXXXX")"
    git show ":2:${path}" > "${merge_dir}/ours"
    git show ":1:${path}" > "${merge_dir}/base"
    git show ":3:${path}" > "${merge_dir}/theirs"
    git merge-file \
      --union \
      "${merge_dir}/ours" \
      "${merge_dir}/base" \
      "${merge_dir}/theirs"
    cp "${merge_dir}/ours" "${path}"
    rm -rf "${merge_dir}"
    git add -- "${path}"
  done
}

restore_merge_authoritative_paths() {
  local release_ref="origin/${RELEASE_BRANCH}"
  local path
  local -a patch_remove_paths=()
  local -a patch_restore_paths=()
  local -a release_remove_paths=()
  local -a release_restore_paths=()

  for path in "${TERMUX_RELEASE_CODE_PATHS[@]}"; do
    if termux_path_in_list "${path}" "${TERMUX_RELEASE_PATCH_AUTHORITATIVE_CODE_PATHS[@]}"; then
      if git cat-file -e "HEAD:${path}" 2>/dev/null; then
        patch_restore_paths+=("${path}")
      elif git ls-files --error-unmatch -- "${path}" >/dev/null 2>&1; then
        patch_remove_paths+=("${path}")
      fi
    elif git cat-file -e "${release_ref}:${path}" 2>/dev/null; then
      release_restore_paths+=("${path}")
    elif git ls-files --error-unmatch -- "${path}" >/dev/null 2>&1; then
      release_remove_paths+=("${path}")
    fi
  done

  if (( ${#patch_restore_paths[@]} > 0 )); then
    git restore \
      --source=HEAD \
      --staged \
      --worktree \
      -- "${patch_restore_paths[@]}"
  fi
  if (( ${#patch_remove_paths[@]} > 0 )); then
    git rm -f --ignore-unmatch -- "${patch_remove_paths[@]}"
  fi

  for path in "${TERMUX_RELEASE_UPSTREAM_AUTHORITATIVE_PATHS[@]}"; do
    if git cat-file -e "${release_ref}:${path}" 2>/dev/null; then
      release_restore_paths+=("${path}")
    elif git ls-files --error-unmatch -- "${path}" >/dev/null 2>&1; then
      release_remove_paths+=("${path}")
    fi
  done

  if (( ${#release_restore_paths[@]} > 0 )); then
    git restore \
      --source="${release_ref}" \
      --staged \
      --worktree \
      -- "${release_restore_paths[@]}"
  fi
  if (( ${#release_remove_paths[@]} > 0 )); then
    git rm -f --ignore-unmatch -- "${release_remove_paths[@]}"
  fi

  resolve_union_merge_paths
}

merge_release_branch_into_work_branch() {
  local release_ref="origin/${RELEASE_BRANCH}"
  local unresolved_paths

  if ! git merge --no-ff --no-commit "${release_ref}"; then
    echo "Release merge reported conflicts; resolving authoritative paths."
  fi

  restore_merge_authoritative_paths
  restore_release_workspace_manifest
  seed_release_branch_workflows
  git add -- "${TERMUX_RELEASE_AUTOMATION_PATHS[@]}"

  unresolved_paths="$(git diff --name-only --diff-filter=U)"
  if [[ -n "${unresolved_paths}" ]]; then
    echo "Unable to merge ${RELEASE_BRANCH} into ${WORK_BRANCH}; unresolved paths remain:" >&2
    printf '%s\n' "${unresolved_paths}" >&2
    exit 1
  fi

  if [[ -f "$(git rev-parse --git-path MERGE_HEAD)" ]]; then
    git commit -m "Merge ${RELEASE_BRANCH} into ${WORK_BRANCH}"
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
    echo "- Patch source: \`${patch_source_label}\`"
    if [[ -n "${pr_url}" ]]; then
      echo "- PR: ${pr_url}"
    fi
  } >> "${GITHUB_STEP_SUMMARY}"
}

enable_release_pr_automerge() {
  local pr_url="$1"
  local pr_info

  if ! pr_info="$(
    gh pr view "${pr_url}" \
      --repo "${GITHUB_REPOSITORY}" \
      --json headRefOid,mergeStateStatus,mergeable,state,url
  )"; then
    echo "::warning::Could not inspect ${pr_url}; leaving release PR auto-merge disabled."
    return 0
  fi

  local pr_state
  pr_state="$(jq -r '.state' <<< "${pr_info}")"
  if [[ "${pr_state}" != "OPEN" ]]; then
    echo "Skipping release PR auto-merge for ${pr_url}; PR is ${pr_state}."
    return 0
  fi

  local mergeable
  local merge_state
  mergeable="$(jq -r '.mergeable // ""' <<< "${pr_info}")"
  merge_state="$(jq -r '.mergeStateStatus // ""' <<< "${pr_info}")"
  if [[ "${mergeable}" == "CONFLICTING" || "${merge_state}" == "DIRTY" ]]; then
    echo "Skipping release PR auto-merge for ${pr_url}; GitHub reports merge conflicts."
    return 0
  fi

  local pr_head_sha
  pr_head_sha="$(jq -r '.headRefOid' <<< "${pr_info}")"

  echo "Enabling auto-merge for release PR ${pr_url}."
  if ! gh pr merge "${pr_url}" \
    --repo "${GITHUB_REPOSITORY}" \
    --squash \
    --auto \
    --delete-branch \
    --match-head-commit "${pr_head_sha}"; then
    echo "::warning::Could not enable auto-merge for ${pr_url}; leaving it for manual merge."
  fi
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

patch_source_ref="origin/${PATCH_BRANCH}"
patch_source_label="${PATCH_BRANCH}"
patch_source_sha="$(git rev-parse "${patch_source_ref}")"

if [[ "${UPSTREAM_TAG}" =~ ^rust-v([0-9]+)\.([0-9]+)\. ]]; then
  release_line="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
  mapfile -t target_termux_tags < <(
    git tag \
      --merged "origin/${PATCH_BRANCH}" \
      --list 'rust-v*-termux' \
      --sort=-v:refname
  )
  if (( ${#target_termux_tags[@]} > 0 )) \
    && [[ "${target_termux_tags[0]}" =~ ^rust-v([0-9]+)\.([0-9]+)\. ]] \
    && [[ "${BASH_REMATCH[1]}.${BASH_REMATCH[2]}" != "${release_line}" ]]; then
    for candidate_tag in "${target_termux_tags[@]}"; do
      if [[ "${candidate_tag}" == "${TERMUX_TAG}" ]]; then
        continue
      fi
      if [[ "${candidate_tag}" == rust-v${release_line}.*-termux ]]; then
        patch_source_ref="refs/tags/${candidate_tag}"
        patch_source_label="${candidate_tag}"
        patch_source_sha="$(git rev-parse "${patch_source_ref}")"
        echo "Using ${candidate_tag} as the patch source because ${PATCH_BRANCH} has advanced to ${target_termux_tags[0]}."
        break
      fi
    done
  fi
fi

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
  if gh release view "${TERMUX_TAG}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
    echo "A matching merged PR already exists for title '${pr_title}': ${existing_pr_url}."
    append_pr_summary "matching merged PR already exists" "${existing_pr_url}"
    exit 0
  fi
  echo "A matching merged PR exists for title '${pr_title}' (${existing_pr_url}), but ${TERMUX_TAG} is missing; rebuilding the release train."
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
existing_open_train_pr_matches_upstream=false
existing_open_train_pr_tag=""
if [[ -n "${existing_open_train_pr}" ]]; then
  existing_open_train_pr_url="$(jq -r '.url' <<< "${existing_open_train_pr}")"
  existing_open_train_pr_head="$(jq -r '.headRefName' <<< "${existing_open_train_pr}")"
  existing_open_train_pr_tag="$(
    jq -r '(.body // "") | try capture("- Upstream tag: `(?<tag>rust-v[^`]+)`").tag catch ""' <<< "${existing_open_train_pr}"
  )"
  if [[ "${existing_open_train_pr_tag}" == "${UPSTREAM_TAG}" ]]; then
    existing_open_train_pr_matches_upstream=true
  fi
  if [[ "${existing_open_train_pr_head}" != "${WORK_BRANCH}" ]]; then
    echo "Open release train PR uses ${existing_open_train_pr_head}; updating that branch instead of ${WORK_BRANCH}."
    WORK_BRANCH="${existing_open_train_pr_head}"
  fi
fi

release_branch_exists=false
if git ls-remote --exit-code --heads origin "${RELEASE_BRANCH}" >/dev/null 2>&1; then
  release_branch_exists=true
  git fetch origin "${RELEASE_BRANCH}"
fi

force_release_branch_push=false
if [[ "${release_branch_exists}" == true ]]; then
  if [[ -n "${existing_open_train_pr_url}" && "${existing_open_train_pr_matches_upstream}" != "true" ]]; then
    echo "Rebuilding ${RELEASE_BRANCH} from ${UPSTREAM_TAG}; open PR ${existing_open_train_pr_url} was for ${existing_open_train_pr_tag:-an unknown upstream tag}."
    release_branch_exists=false
    force_release_branch_push=true
  else
    delete_existing_release_branch_if_safe "${RELEASE_BRANCH}"
    if ! git ls-remote --exit-code --heads origin "${RELEASE_BRANCH}" >/dev/null 2>&1; then
      release_branch_exists=false
    fi
  fi
fi

if [[ "${release_branch_exists}" == false ]]; then
  git checkout -B "${RELEASE_BRANCH}" "refs/tags/${UPSTREAM_TAG}"
  seed_release_branch_workflows
  apply_seeded_release_code_patches
  git_add_seeded_release_paths
  if ! git diff --cached --quiet; then
    git commit -m "Seed Termux release automation"
  fi
  if [[ "${force_release_branch_push}" == "true" ]]; then
    git push --force-with-lease origin "${RELEASE_BRANCH}"
  else
    git push origin "${RELEASE_BRANCH}"
  fi
fi

git fetch origin "${RELEASE_BRANCH}"
work_branch_exists=false
if git ls-remote --exit-code --heads origin "${WORK_BRANCH}" >/dev/null 2>&1; then
  work_branch_exists=true
  git fetch origin "${WORK_BRANCH}"
fi

if [[ "${work_branch_exists}" == true \
  && -n "${existing_open_train_pr_url}" \
  && "${existing_open_train_pr_matches_upstream}" == "true" \
  && "${patch_source_ref}" == "origin/${PATCH_BRANCH}" ]]; then
  git checkout -B "${WORK_BRANCH}" "origin/${WORK_BRANCH}"
else
  git checkout -B "${WORK_BRANCH}" "${patch_source_ref}"
fi
normalize_workspace_version_to_upstream_tag
git add -- codex-rs/Cargo.toml
if ! git diff --cached --quiet; then
  git commit -m "Normalize workspace version for ${UPSTREAM_TAG}"
fi
merge_release_branch_into_work_branch
seed_release_branch_workflows
# The patch branch is the authoritative Termux code source. Reapplying the
# release code patch here can conflict when that branch contains newer upstream
# context around the same Termux changes.
normalize_workspace_version_to_upstream_tag

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
  --arg patch_source_sha "${patch_source_sha}" \
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
  echo "- Patch source: \`${patch_source_label}\`"
  echo
  echo "This PR is intentionally created from \`${patch_source_label}\` with the Termux release automation files copied from \`dev\`, then targeted at the upstream release branch. If GitHub reports conflicts, resolve them manually by keeping the upstream release code while preserving the Termux compatibility fixes."
  echo
  echo "Auto-merge is enabled when GitHub reports the PR as mergeable. Required CI, including the Termux artifact smoke test, is the approval gate. After merge, the deployment workflow attaches the tested artifact to \`${TERMUX_TAG}\` and opens the checkpoint PR."
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
enable_release_pr_automerge "${pr_url}"
echo "pr_url=${pr_url}" >> "$GITHUB_OUTPUT"
append_pr_summary "${pr_action} release train PR" "${pr_url}"
