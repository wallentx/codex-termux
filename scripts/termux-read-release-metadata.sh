#!/usr/bin/env bash

# Parse .github/termux-release.json and emit workflow outputs for deploy or
# promote jobs. TERMUX_RELEASE_ACTION must be "deploy" or "promote".

set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

metadata=".github/termux-release.json"
action="${TERMUX_RELEASE_ACTION:-deploy}"
case "${action}" in
  deploy)
    missing_context="deployment"
    ;;
  promote)
    missing_context="promotion"
    ;;
  *)
    echo "TERMUX_RELEASE_ACTION must be deploy or promote." >&2
    exit 1
    ;;
esac

if [[ ! -f "${metadata}" ]]; then
  echo "No ${metadata}; this push is not a Termux release ${missing_context}."
  echo "${action}=false" >> "${GITHUB_OUTPUT}"
  exit 0
fi

upstream_tag="$(jq -r '.upstream_tag // empty' "${metadata}")"
upstream_name="$(jq -r '.upstream_name // .upstream_tag // empty' "${metadata}")"
termux_tag="$(jq -r '.termux_tag // empty' "${metadata}")"
upstream_version="${upstream_tag#rust-v}"
upstream_version="${upstream_version%-termux}"
upstream_prerelease=false
if [[ "${upstream_version}" == *-* ]]; then
  upstream_prerelease=true
fi
upstream_html_url="$(jq -r '.upstream_html_url // ""' "${metadata}")"
upstream_repo="$(jq -r '.upstream_repo // "openai/codex"' "${metadata}")"
release_train="$(jq -r '.release_train // ""' "${metadata}")"
if [[ -z "${upstream_tag}" || -z "${termux_tag}" ]]; then
  echo "Missing upstream_tag or termux_tag in ${metadata}" >&2
  exit 1
fi

release_state="$(TERMUX_TAG="${termux_tag}" "${script_dir}/termux-release-asset-state.sh")"
release_exists="$(awk -F= '$1 == "release_exists" { print $2 }' <<< "${release_state}")"
asset_exists="$(awk -F= '$1 == "asset_exists" { print $2 }' <<< "${release_state}")"

if [[ "${action}" == "promote" ]]; then
  if [[ "${asset_exists}" == "true" ]]; then
    echo "${termux_tag} already exists with codex-aarch64-linux-android.tar.gz; skipping promotion."
    echo "promote=false" >> "${GITHUB_OUTPUT}"
    exit 0
  fi
  if [[ "${release_exists}" == "true" ]]; then
    echo "${termux_tag} exists but is missing codex-aarch64-linux-android.tar.gz; repairing promotion."
  fi
fi

{
  echo "${action}=true"
  echo "upstream_tag=${upstream_tag}"
  echo "upstream_name=${upstream_name}"
  echo "termux_tag=${termux_tag}"
  echo "upstream_prerelease=${upstream_prerelease}"
  echo "upstream_html_url=${upstream_html_url}"
  echo "upstream_repo=${upstream_repo}"
  echo "release_train=${release_train}"
  echo "release_exists=${release_exists}"
  echo "asset_exists=${asset_exists}"
} >> "${GITHUB_OUTPUT}"
