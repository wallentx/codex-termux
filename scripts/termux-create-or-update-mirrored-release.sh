#!/usr/bin/env bash

# Create a mirrored Termux release or repair an existing release that is missing
# the Android tarball.

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${UPSTREAM_TAG:?UPSTREAM_TAG is required}"
: "${UPSTREAM_REPO:?UPSTREAM_REPO is required}"
: "${UPSTREAM_NAME?UPSTREAM_NAME is required}"
: "${TERMUX_TAG:?TERMUX_TAG is required}"
: "${UPSTREAM_PRERELEASE:?UPSTREAM_PRERELEASE is required}"
: "${UPSTREAM_HTML_URL?UPSTREAM_HTML_URL is required}"
: "${RELEASE_TRAIN?RELEASE_TRAIN is required}"
: "${RELEASE_EXISTS:?RELEASE_EXISTS is required}"
: "${PR_NUMBER:?PR_NUMBER is required}"
: "${HEAD_SHA:?HEAD_SHA is required}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"

promoted_dir="${PROMOTED_DIR:-promoted}"
asset_path="${promoted_dir}/codex-aarch64-linux-android.tar.gz"

body_path="${RUNNER_TEMP}/release-body.md"
upstream_body_path="${RUNNER_TEMP}/upstream-release-body.md"
upstream_body_without_changelog_path="${RUNNER_TEMP}/upstream-release-body-without-changelog.md"
if gh release view "${UPSTREAM_TAG}" \
  --repo "${UPSTREAM_REPO}" \
  --json body \
  --jq '.body // ""' > "${upstream_body_path}"; then
  awk '
    function heading_level(line, text) {
      if (match(line, /^(#{1,6})[[:space:]]+(.+)$/, parts)) {
        text = tolower(parts[2])
        sub(/[[:space:]]+#+[[:space:]]*$/, "", text)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", text)
        if (text == "changelog" || text == "change log") {
          return length(parts[1])
        }
      }
      return 0
    }

    {
      if (!skip) {
        level = heading_level($0)
        if (level > 0) {
          skip = 1
          skip_level = level
          next
        }
        print
        next
      }

      if (match($0, /^(#{1,6})[[:space:]]+/, parts) && length(parts[1]) <= skip_level) {
        skip = 0
        print
      }
    }
  ' "${upstream_body_path}" > "${upstream_body_without_changelog_path}"
else
  echo "::warning title=Upstream release notes unavailable::Could not read ${UPSTREAM_REPO} release ${UPSTREAM_TAG}."
  : > "${upstream_body_without_changelog_path}"
fi

{
  echo "Termux Android build for ${UPSTREAM_TAG}."
  echo
  echo "- Upstream release: ${UPSTREAM_HTML_URL}"
  echo "- Release train: \`${RELEASE_TRAIN}\`"
  echo "- Promoted PR: #${PR_NUMBER}"
  echo "- Promoted PR head SHA: \`${HEAD_SHA}\`"
} > "${body_path}"
if [[ -s "${upstream_body_without_changelog_path}" ]]; then
  {
    echo
    echo "## Upstream release notes"
    echo
    cat "${upstream_body_without_changelog_path}"
  } >> "${body_path}"
fi

release_title="${UPSTREAM_NAME}"
if [[ -z "${release_title}" || "${release_title}" == "null" ]]; then
  release_title="${TERMUX_TAG}"
fi

if [[ "${RELEASE_EXISTS}" == "true" ]]; then
  gh release upload \
    "${TERMUX_TAG}" \
    "${asset_path}#codex-termux" \
    --repo "${GITHUB_REPOSITORY}" \
    --clobber
  exit 0
fi

release_args=(
  gh release create "${TERMUX_TAG}"
  --repo "${GITHUB_REPOSITORY}"
  --target "${RELEASE_SHA}"
  --title "${release_title}"
  --notes-file "${body_path}"
)
if [[ "${UPSTREAM_PRERELEASE}" == "true" ]]; then
  release_args+=(--prerelease)
fi
release_args+=("${asset_path}#codex-termux")
"${release_args[@]}"
