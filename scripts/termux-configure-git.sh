#!/usr/bin/env bash

# Configure the explicit bot identity used by release automation and fetch any
# refs requested by the calling workflow.

set -euo pipefail

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

while (($#)); do
  case "$1" in
    --origin)
      shift
      origin_refs=()
      while (($#)) && [[ "$1" != --* ]]; do
        origin_refs+=("$1")
        shift
      done
      if ((${#origin_refs[@]})); then
        git fetch --prune origin "${origin_refs[@]}"
      fi
      ;;
    --upstream-tag)
      if (($# < 3)); then
        echo "--upstream-tag requires <repo> <tag>." >&2
        exit 1
      fi
      upstream_repo="$2"
      upstream_tag="$3"
      git remote add upstream "https://github.com/${upstream_repo}.git" 2>/dev/null || true
      git fetch --prune --no-tags upstream "+refs/tags/${upstream_tag}:refs/tags/${upstream_tag}"
      shift 3
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done
