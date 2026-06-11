#!/usr/bin/env bash

# Resolve the release branch and SHA for deploy/promote workflows.

set -euo pipefail

: "${GITHUB_EVENT_NAME:?GITHUB_EVENT_NAME is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"

input_release_branch="${INPUT_RELEASE_BRANCH:-${REQUESTED_RELEASE_BRANCH:-}}"
input_release_sha="${INPUT_RELEASE_SHA:-${REQUESTED_RELEASE_SHA:-}}"

if [[ "${GITHUB_EVENT_NAME}" == "workflow_dispatch" ]]; then
  if [[ -z "${input_release_branch}" ]]; then
    echo "release_branch is required for workflow_dispatch." >&2
    exit 1
  fi

  release_branch="${input_release_branch}"
  if [[ -n "${input_release_sha}" ]]; then
    git checkout --detach "${input_release_sha}"
    release_sha="${input_release_sha}"
  else
    release_sha="$(git rev-parse HEAD)"
  fi
else
  : "${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"
  : "${GITHUB_SHA:?GITHUB_SHA is required}"
  release_branch="${GITHUB_REF_NAME}"
  release_sha="${GITHUB_SHA}"
fi

{
  echo "branch=${release_branch}"
  echo "sha=${release_sha}"
} >> "${GITHUB_OUTPUT}"
