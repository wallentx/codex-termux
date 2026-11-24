#!/usr/bin/env bash

# Lightweight post-toolbox check for jobs that rely on authenticated gh calls.

set -euo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

command -v gh
gh auth status --hostname github.com

printf 'GITHUB_REPOSITORY=%s\n' "${GITHUB_REPOSITORY}"
printf 'REPO=%s\n' "${REPO:-}"
printf 'GH_REPO_URL=%s\n' "${GH_REPO_URL:-}"
printf 'GH_WORKFLOW_URL=%s\n' "${GH_WORKFLOW_URL:-}"
