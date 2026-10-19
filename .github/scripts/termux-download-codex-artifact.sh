#!/usr/bin/env bash
set -Eeuo pipefail

download_dir="${DOWNLOAD_DIR:-termux-smoke-artifact}"
release_tag="${RELEASE_TAG:-}"
artifact_url="${ARTIFACT_URL:-}"
artifact_run_id="${ARTIFACT_RUN_ID:-}"
artifact_name="${ARTIFACT_NAME:-}"
repo="${GITHUB_REPOSITORY:-wallentx/codex-termux}"
workspace="${GITHUB_WORKSPACE:-$PWD}"

rm -rf "$download_dir"
mkdir -p "$download_dir"

source_count=0
[[ -n "$release_tag" ]] && ((source_count += 1))
[[ -n "$artifact_url" ]] && ((source_count += 1))
[[ -n "$artifact_name" ]] && ((source_count += 1))

if (( source_count != 1 )); then
    echo "Specify exactly one artifact source: RELEASE_TAG, ARTIFACT_URL, or ARTIFACT_NAME." >&2
    exit 1
fi

if [[ -n "$release_tag" ]]; then
    echo "Downloading Android release assets from ${repo}@${release_tag}"
    gh release download "$release_tag" \
        --repo "$repo" \
        --dir "$download_dir" \
        --pattern '*aarch64-linux-android*.tar.gz'
elif [[ -n "$artifact_url" ]]; then
    if [[ -z "${GH_TOKEN:-}" ]]; then
        echo "GH_TOKEN is required to download a GitHub Actions artifact URL." >&2
        exit 1
    fi
    if [[ ! "$artifact_url" =~ github\.com/([^/]+/[^/]+)/actions/runs/([0-9]+)/artifacts/([0-9]+) ]]; then
        echo "Unsupported artifact URL: $artifact_url" >&2
        exit 1
    fi

    artifact_repo="${BASH_REMATCH[1]}"
    artifact_id="${BASH_REMATCH[3]}"
    artifact_zip="${download_dir}/artifact-${artifact_id}.zip"
    echo "Downloading Actions artifact ${artifact_id} from ${artifact_repo}"
    curl -fsSL \
        -H "Authorization: Bearer ${GH_TOKEN}" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        -o "$artifact_zip" \
        "https://api.github.com/repos/${artifact_repo}/actions/artifacts/${artifact_id}/zip"
    unzip -q "$artifact_zip" -d "$download_dir"
    rm -f "$artifact_zip"
else
    if [[ -z "$artifact_run_id" ]]; then
        echo "ARTIFACT_RUN_ID is required when ARTIFACT_NAME is used." >&2
        exit 1
    fi

    echo "Downloading Actions artifact ${artifact_name} from run ${artifact_run_id}"
    gh run download "$artifact_run_id" \
        --repo "$repo" \
        --name "$artifact_name" \
        --dir "$download_dir"
fi

archive="$(
    find "$download_dir" -type f -name 'codex-aarch64-linux-android.tar.gz' -print -quit
)"
if [[ -z "$archive" ]]; then
    archive="$(
        find "$download_dir" -type f -name 'codex-package-aarch64-linux-android.tar.gz' -print -quit
    )"
fi
if [[ -z "$archive" ]]; then
    archive="$(
        find "$download_dir" -type f -name '*aarch64-linux-android*.tar.gz' -print -quit
    )"
fi
if [[ -z "$archive" ]]; then
    echo "Could not find an aarch64-linux-android .tar.gz artifact in ${download_dir}." >&2
    find "$download_dir" -maxdepth 4 -type f -print >&2
    exit 1
fi

case "$archive" in
    "$workspace"/*)
        archive_output="${archive#"$workspace"/}"
        ;;
    *)
        archive_output="$archive"
        ;;
esac

echo "Selected smoke-test archive: ${archive_output}"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "archive_path=${archive_output}" >> "$GITHUB_OUTPUT"
fi
