#!/usr/bin/env bash

set -euo pipefail

if [[ -z "${HEAD_SHA:-}" ]]; then
  echo "HEAD_SHA is required." >&2
  exit 1
fi

if [[ -z "${PR_ARTIFACT_NAME:-}" ]]; then
  echo "PR_ARTIFACT_NAME is required." >&2
  exit 1
fi

promoted_dir="${PROMOTED_DIR:-promoted}"

find_run_id() {
  local event="$1"
  gh run list \
    --repo "${GITHUB_REPOSITORY}" \
    --workflow rust-release.yml \
    --event "${event}" \
    --status success \
    --commit "${HEAD_SHA}" \
    --limit 1 \
    --json databaseId \
    --jq '.[0].databaseId // empty'
}

artifact_name="${PR_ARTIFACT_NAME}"
run_id="$(find_run_id pull_request)"
mkdir -p "${promoted_dir}"
if [[ -n "${run_id}" ]] && gh run download "${run_id}" \
  --repo "${GITHUB_REPOSITORY}" \
  --name "${artifact_name}" \
  --dir "${promoted_dir}"; then
  :
else
  artifact_name="aarch64-linux-android"
  run_id="$(find_run_id workflow_dispatch)"
  if [[ -z "${run_id}" ]]; then
    echo "No successful rust-release run found for ${HEAD_SHA}" >&2
    exit 1
  fi
  gh run download "${run_id}" \
    --repo "${GITHUB_REPOSITORY}" \
    --name "${artifact_name}" \
    --dir "${promoted_dir}"
fi

ls -la "${promoted_dir}"
if [[ -f "${promoted_dir}/SHA256SUMS" ]]; then
  (cd "${promoted_dir}" && sha256sum -c SHA256SUMS)
fi
if [[ ! -f "${promoted_dir}/codex-aarch64-linux-android.tar.gz" ]]; then
  echo "Expected ${promoted_dir}/codex-aarch64-linux-android.tar.gz in the downloaded artifact." >&2
  exit 1
fi
