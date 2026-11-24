#!/usr/bin/env bash

set -euo pipefail

termux_tag="${1:-${TERMUX_TAG:-}}"
release_repo="${RELEASE_REPO:-${GITHUB_REPOSITORY:-}}"
asset_name="${ASSET_NAME:-codex-aarch64-linux-android.tar.gz}"

if [[ -z "${termux_tag}" ]]; then
  echo "TERMUX_TAG or tag argument is required." >&2
  exit 1
fi

if [[ -z "${release_repo}" ]]; then
  echo "GITHUB_REPOSITORY or RELEASE_REPO is required." >&2
  exit 1
fi

release_exists=false
asset_exists=false

if gh release view "${termux_tag}" --repo "${release_repo}" >/dev/null 2>&1; then
  release_exists=true
  release_asset_exists="$(
    gh release view "${termux_tag}" \
      --repo "${release_repo}" \
      --json assets \
      | jq -r --arg asset_name "${asset_name}" \
        '.assets | map(.name) | any(. == $asset_name)'
  )"
  if [[ "${release_asset_exists}" == "true" ]]; then
    asset_exists=true
  fi
fi

printf 'release_exists=%s\n' "${release_exists}"
printf 'asset_exists=%s\n' "${asset_exists}"
