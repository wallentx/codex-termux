#!/usr/bin/env bash

# Delete completed release train branches only after their Termux release and
# Android artifact exist and no open PR still references the branch.

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

git fetch --prune origin '+refs/heads/release/*:refs/remotes/origin/release/*'

open_prs="$(
  gh pr list \
    --repo "${GITHUB_REPOSITORY}" \
    --state open \
    --limit 200 \
    --json number,title,body,headRefName,baseRefName,url
)"

mapfile -t release_refs < <(
  git for-each-ref --format='%(refname:short)' refs/remotes/origin/release
)

if ((${#release_refs[@]} == 0)); then
  echo "No release branches found."
  exit 0
fi

for release_ref in "${release_refs[@]}"; do
  release_branch="${release_ref#origin/}"

  metadata="$(git show "${release_ref}:.github/termux-release.json" 2>/dev/null || true)"
  if [[ -z "${metadata}" ]]; then
    echo "Keeping ${release_branch}: no .github/termux-release.json metadata."
    continue
  fi

  termux_tag="$(jq -r '.termux_tag // empty' <<< "${metadata}")"
  if [[ -z "${termux_tag}" ]]; then
    echo "Keeping ${release_branch}: metadata does not include termux_tag."
    continue
  fi

  release_state="$(TERMUX_TAG="${termux_tag}" "${script_dir}/termux-release-asset-state.sh")"
  release_exists="$(awk -F= '$1 == "release_exists" { print $2 }' <<< "${release_state}")"
  asset_exists="$(awk -F= '$1 == "asset_exists" { print $2 }' <<< "${release_state}")"

  if [[ "${release_exists}" != "true" ]]; then
    echo "Keeping ${release_branch}: release ${termux_tag} does not exist yet."
    continue
  fi
  if [[ "${asset_exists}" != "true" ]]; then
    echo "Keeping ${release_branch}: release ${termux_tag} is missing codex-aarch64-linux-android.tar.gz."
    continue
  fi

  blocking_prs="$(
    jq -r --arg branch "${release_branch}" '
      .[]
      | select(
          .baseRefName == $branch
          or .headRefName == $branch
          or (.title | contains($branch))
          or ((.body // "") | contains($branch))
        )
      | "\(.url) #\(.number)"
    ' <<< "${open_prs}"
  )"
  if [[ -n "${blocking_prs}" ]]; then
    echo "Keeping ${release_branch}: open PRs still reference it."
    printf '%s\n' "${blocking_prs}"
    continue
  fi

  echo "Deleting completed release branch ${release_branch} for ${termux_tag}."
  if ! git push origin --delete "${release_branch}"; then
    echo "::warning::Failed to delete ${release_branch}; continuing cleanup."
  fi
done
