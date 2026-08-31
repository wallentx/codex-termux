#!/usr/bin/env bash

set -euo pipefail

# Required env: GITHUB_REPOSITORY, UPSTREAM_REPO, REQUESTED_TAG,
# BYPASS_PRIOR_RELEASE_TRAIN, GITHUB_OUTPUT. GH_TOKEN must authenticate gh.
# Outputs match the former workflow step outputs.

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${UPSTREAM_REPO:?UPSTREAM_REPO is required}"
: "${REQUESTED_TAG?REQUESTED_TAG is required}"
: "${BYPASS_PRIOR_RELEASE_TRAIN:?BYPASS_PRIOR_RELEASE_TRAIN is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

candidate_release_branch() {
  local tag="$1"
  local version="${tag#rust-v}"
  version="${version%-termux}"
  local release_train="${version%%-*}"
  printf 'release/%s\n' "${release_train}"
}

normalize_rust_tag_version() {
  local tag="$1"
  local version="${tag#rust-v}"
  version="${version%-termux}"
  printf '%s\n' "${version}"
}

termux_release_tags_cache_loaded=false
termux_release_tags_cache=""
termux_release_tags() {
  if [[ "${termux_release_tags_cache_loaded}" != "true" ]]; then
    termux_release_tags_cache="$(
      gh release list \
        --repo "${GITHUB_REPOSITORY}" \
        --exclude-drafts \
        --limit 200 \
        --json tagName \
        --jq '.[].tagName'
    )"
    termux_release_tags_cache_loaded=true
  fi
  printf '%s\n' "${termux_release_tags_cache}"
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

normalize_upstream_release_json() {
  jq -c '
    def normalize:
      {
        tagName: (.tag_name // .tagName // ""),
        name: (.name // .tag_name // .tagName // ""),
        body: (.body // ""),
        url: (.html_url // .url // ""),
        isPrerelease: (.prerelease // .isPrerelease // false),
        targetCommitish: (.target_commitish // .targetCommitish // ""),
        databaseId: (.id // .databaseId // "")
      };
    if type == "array" then map(normalize) else normalize end
  '
}

upstream_releases_cache_loaded=false
upstream_releases_cache="[]"
upstream_releases_json() {
  if [[ "${upstream_releases_cache_loaded}" != "true" ]]; then
    upstream_releases_cache="$(
      gh api \
        --method GET \
        "repos/${UPSTREAM_REPO}/releases?per_page=30" \
        | normalize_upstream_release_json
    )"
    upstream_releases_cache_loaded=true
  fi
  printf '%s\n' "${upstream_releases_cache}"
}

# Scheduled runs track channel heads only. Older tags remain available through REQUESTED_TAG.
scheduled_upstream_releases_json() {
  upstream_releases_json \
    | jq -c '
        ([.[] | select(.tagName | startswith("rust-v")) | select(.isPrerelease == false)][0].tagName // "") as $latest_stable
        | ([.[] | select(.tagName | startswith("rust-v")) | select(.isPrerelease == true)][0].tagName // "") as $latest_prerelease
        | ([.[] | select(.tagName | startswith("rusty-v8-v"))][0].tagName // "") as $latest_rusty_v8
        | [
            .[]
            | select(
                .tagName == $latest_stable
                or .tagName == $latest_prerelease
                or .tagName == $latest_rusty_v8
              )
          ]
      '
}

release_branch_current_tag() {
  local release_branch="$1"
  release_branch_current_metadata "${release_branch}" \
    | jq -r '.upstream_tag // .termux_tag // empty'
}

release_branch_current_metadata() {
  local release_branch="$1"
  git show "origin/${release_branch}:.github/termux-release.json" 2>/dev/null || true
}

release_branch_current_termux_tag() {
  local release_branch="$1"
  release_branch_current_metadata "${release_branch}" \
    | jq -r '.termux_tag // empty'
}

open_release_train_pr_for_branch() {
  local release_branch="$1"
  local open_prs

  open_prs="$(open_prs_json)"

  jq -c \
    --arg release_branch "${release_branch}" \
    '
      [
        .[]
        | select(.baseRefName == $release_branch)
        | select(
            ((.title // "") | startswith("Termux rust-v"))
            and ((.body // "") | contains("Release train branch: `" + $release_branch + "`"))
          )
        | . + {
            upstreamTag: (
              (.body // "")
              | try capture("- Upstream tag: `(?<tag>rust-v[^`]+)`").tag catch ""
            )
          }
      ]
      | sort_by(.number)
      | reverse
      | .[0] // empty
    ' <<< "${open_prs}"
}

open_other_release_train_prs() {
  local release_branch="$1"
  local open_prs

  open_prs="$(open_prs_json)"

  jq -r \
    --arg release_branch "${release_branch}" \
    '
      .[]
      | select(.baseRefName != $release_branch)
      | select(
          ((.title // "") | startswith("Termux rust-v"))
          and ((.body // "") | contains("Release train branch: `release/"))
        )
      | "- #\(.number) \(.title) (\(.url))"
    ' <<< "${open_prs}"
}

rust_tag_is_newer_than_tag() {
  local candidate_tag="$1"
  local current_tag="$2"
  local candidate_version
  local current_version
  local candidate_base
  local current_base
  local candidate_is_prerelease=false
  local current_is_prerelease=false
  local newest_version

  if [[ -z "${current_tag}" ]]; then
    return 0
  fi

  candidate_version="$(normalize_rust_tag_version "${candidate_tag}")"
  current_version="$(normalize_rust_tag_version "${current_tag}")"
  candidate_base="${candidate_version%%-*}"
  current_base="${current_version%%-*}"

  if [[ "${candidate_version}" == *-* ]]; then
    candidate_is_prerelease=true
  fi
  if [[ "${current_version}" == *-* ]]; then
    current_is_prerelease=true
  fi

  if [[ "${candidate_base}" == "${current_base}" ]]; then
    if [[ "${current_is_prerelease}" == true && "${candidate_is_prerelease}" == false ]]; then
      return 0
    fi
    if [[ "${current_is_prerelease}" == false && "${candidate_is_prerelease}" == true ]]; then
      return 1
    fi
  fi

  newest_version="$(
    printf '%s\n%s\n' "${current_version}" "${candidate_version}" \
      | sort -V \
      | tail -n 1
  )"
  [[ "${newest_version}" == "${candidate_version}" && "${candidate_version}" != "${current_version}" ]]
}

latest_mirrored_termux_tag_for_train() {
  local release_train="$1"
  local newest_tag=""
  local termux_tag
  local termux_version
  local termux_train

  while IFS= read -r termux_tag; do
    [[ -n "${termux_tag}" ]] || continue
    case "${termux_tag}" in
      rust-v*-termux)
        ;;
      *)
        continue
        ;;
    esac

    termux_version="$(normalize_rust_tag_version "${termux_tag}")"
    termux_train="${termux_version%%-*}"
    [[ "${termux_train}" == "${release_train}" ]] || continue

    if [[ -z "${newest_tag}" ]] || rust_tag_is_newer_than_tag "${termux_tag}" "${newest_tag}"; then
      newest_tag="${termux_tag}"
    fi
  done < <(termux_release_tags)

  printf '%s\n' "${newest_tag}"
}

release_tag_is_newer_than_known_train() {
  local candidate_tag="$1"
  local release_branch="$2"
  local open_train_pr_json="${3:-}"
  local current_tag
  local mirrored_tag
  local open_pr_tag

  current_tag="$(release_branch_current_tag "${release_branch}")"
  if ! rust_tag_is_newer_than_tag "${candidate_tag}" "${current_tag}"; then
    echo "${candidate_tag} is not newer than ${current_tag} already recorded on ${release_branch}; nothing to do."
    return 1
  fi

  mirrored_tag="$(latest_mirrored_termux_tag_for_train "${release_branch#release/}")"
  if ! rust_tag_is_newer_than_tag "${candidate_tag}" "${mirrored_tag}"; then
    echo "${candidate_tag} is not newer than ${mirrored_tag} already mirrored for ${release_branch}; nothing to do."
    return 1
  fi

  if [[ -n "${open_train_pr_json}" ]]; then
    open_pr_tag="$(jq -r '.upstreamTag // empty' <<< "${open_train_pr_json}")"
    if ! rust_tag_is_newer_than_tag "${candidate_tag}" "${open_pr_tag}"; then
      echo "${candidate_tag} is not newer than ${open_pr_tag} already proposed in open PR $(jq -r '.url' <<< "${open_train_pr_json}"); nothing to do."
      return 1
    fi
  fi

  return 0
}

release_json_for_tag() {
  local tag="$1"
  local release_json

  release_json="$(
    upstream_releases_json \
      | jq -c --arg tag "${tag}" '.[] | select(.tagName == $tag)' \
      | head -n 1
  )"
  if [[ -n "${release_json}" ]]; then
    printf '%s\n' "${release_json}"
    return 0
  fi

  gh api \
    --method GET \
    "repos/${UPSTREAM_REPO}/releases/tags/${tag}" \
    | normalize_upstream_release_json
}

append_selection_summary() {
  local selected="$1"
  local release_kind="${2:-}"
  local upstream_tag="${3:-}"
  local release_train="${4:-}"
  local release_branch="${5:-}"
  local work_branch="${6:-}"
  local termux_tag="${7:-}"
  local short_repo="${REPO:-${GITHUB_REPOSITORY##*/}}"
  local repo_url="${GH_REPO_URL:-https://github.com/${GITHUB_REPOSITORY}}"

  [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] || return 0

  {
    echo "## Termux release selection"
    echo
    echo "- Repository: [${short_repo}](${repo_url})"
    if [[ -n "${GH_WORKFLOW_URL:-}" ]]; then
      echo "- Workflow run: ${GH_WORKFLOW_URL}"
    fi

    if [[ "${selected}" == "true" ]]; then
      echo "- Selected upstream tag: \`${upstream_tag}\`"
      echo "- Release kind: \`${release_kind}\`"
      if [[ -n "${release_train}" ]]; then
        echo "- Release train: \`${release_train}\`"
      fi
      if [[ -n "${release_branch}" ]]; then
        echo "- Release train branch: \`${release_branch}\`"
      fi
      if [[ -n "${work_branch}" ]]; then
        echo "- Work branch: \`${work_branch}\`"
      fi
      if [[ -n "${termux_tag}" ]]; then
        echo "- Termux tag: \`${termux_tag}\`"
      fi
    else
      echo "- Selected: no"
      echo "- Outcome: no upstream Codex or rusty_v8 release needs a Termux mirror."
    fi
  } >> "${GITHUB_STEP_SUMMARY}"
}

emit_selected_release() {
  local release_kind="$1"
  local release_json="$2"
  local release_train="${3:-}"
  local release_branch="${4:-}"
  local work_branch="${5:-}"
  local termux_tag="${6:-}"
  local upstream_tag
  local upstream_name
  local upstream_body
  local upstream_html_url
  local upstream_prerelease
  local upstream_target
  local upstream_id

  upstream_tag="$(jq -r '.tagName // empty' <<< "${release_json}")"
  upstream_name="$(jq -r '.name // .tagName' <<< "${release_json}")"
  upstream_body="$(jq -r '.body // ""' <<< "${release_json}")"
  upstream_html_url="$(jq -r '.url' <<< "${release_json}")"
  upstream_prerelease="$(jq -r '.isPrerelease' <<< "${release_json}")"
  upstream_target="$(jq -r '.targetCommitish // ""' <<< "${release_json}")"
  upstream_id="$(jq -r '.databaseId // ""' <<< "${release_json}")"

  {
    echo "selected=true"
    echo "release_kind=${release_kind}"
    echo "upstream_tag=${upstream_tag}"
    echo "upstream_name=${upstream_name}"
    echo "upstream_html_url=${upstream_html_url}"
    echo "upstream_prerelease=${upstream_prerelease}"
    echo "upstream_target=${upstream_target}"
    echo "upstream_id=${upstream_id}"
    echo "release_train=${release_train}"
    echo "release_branch=${release_branch}"
    echo "work_branch=${work_branch}"
    echo "termux_tag=${termux_tag}"
  } >> "$GITHUB_OUTPUT"

  body_delimiter="termux_release_body_$(date +%s%N)_${RANDOM}_${RANDOM}"
  while grep -qxF "${body_delimiter}" <<< "${upstream_body}"; do
    body_delimiter="termux_release_body_$(date +%s%N)_${RANDOM}_${RANDOM}"
  done
  {
    echo "body<<${body_delimiter}"
    printf '%s\n' "${upstream_body}"
    echo "${body_delimiter}"
  } >> "$GITHUB_OUTPUT"

  append_selection_summary \
    "true" \
    "${release_kind}" \
    "${upstream_tag}" \
    "${release_train}" \
    "${release_branch}" \
    "${work_branch}" \
    "${termux_tag}"
}

maybe_select_rusty_v8_release() {
  local release_json="$1"
  local upstream_tag

  upstream_tag="$(jq -r '.tagName // empty' <<< "${release_json}")"
  if [[ "${upstream_tag}" != rusty-v8-v* ]]; then
    return 1
  fi

  if gh release view "${upstream_tag}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
    echo "${upstream_tag} already exists in ${GITHUB_REPOSITORY}; nothing to do."
    return 1
  fi

  emit_selected_release "rusty-v8" "${release_json}" "" "" "" "${upstream_tag}"
  return 0
}

maybe_select_codex_release() {
  local release_json="$1"
  local upstream_tag
  local version
  local release_train
  local release_branch
  local work_branch
  local termux_tag
  local termux_release_exists=false
  local current_tag
  local open_train_pr_json
  local open_train_pr_tag
  local pending_other_release_train_prs
  local rebuild_requested_open_train=false

  upstream_tag="$(jq -r '.tagName // empty' <<< "${release_json}")"
  if [[ "${upstream_tag}" != rust-v* ]]; then
    return 1
  fi

  version="${upstream_tag#rust-v}"
  version="${version%-termux}"
  release_train="${version%%-*}"
  release_branch="release/${release_train}"
  work_branch="upstream/rust-v${release_train}"
  termux_tag="${upstream_tag}-termux"
  if gh release view "${termux_tag}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
    termux_release_exists=true
  fi
  open_train_pr_json="$(open_release_train_pr_for_branch "${release_branch}")"
  if [[ -n "${open_train_pr_json}" ]]; then
    work_branch="$(jq -r '.headRefName' <<< "${open_train_pr_json}")"
    open_train_pr_tag="$(jq -r '.upstreamTag // empty' <<< "${open_train_pr_json}")"
    if [[ -n "${REQUESTED_TAG}" \
      && "${open_train_pr_tag}" == "${upstream_tag}" \
      && "${termux_release_exists}" != "true" ]]; then
      rebuild_requested_open_train=true
    fi
  fi

  if [[ -z "${REQUESTED_TAG}" && "${version}" == *-* ]]; then
    stable_termux_tag="rust-v${release_train}-termux"
    if gh release view "${stable_termux_tag}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
      echo "${upstream_tag} is a prerelease for ${release_train}, but ${stable_termux_tag} already exists; nothing to do."
      return 1
    fi
  fi

  if [[ "${rebuild_requested_open_train}" == "true" ]]; then
    echo "Manual dispatch requested a rebuild of ${upstream_tag} in open PR $(jq -r '.url' <<< "${open_train_pr_json}")."
  elif ! release_tag_is_newer_than_known_train "${upstream_tag}" "${release_branch}" "${open_train_pr_json}"; then
    current_tag="$(release_branch_current_tag "${release_branch}")"
    if [[ "${termux_release_exists}" == "true" || "${current_tag}" != "${upstream_tag}" || -n "${open_train_pr_json}" ]]; then
      return 1
    fi
    echo "${termux_tag} is missing even though ${release_branch} records ${upstream_tag}; recreating the release train."
  elif [[ "${termux_release_exists}" == "true" ]]; then
    echo "${termux_tag} already exists; nothing to do."
    return 1
  fi

  if [[ "${BYPASS_PRIOR_RELEASE_TRAIN}" != "true" ]]; then
    pending_other_release_train_prs="$(open_other_release_train_prs "${release_branch}")"
    if [[ -n "${pending_other_release_train_prs}" ]]; then
      echo "${upstream_tag} is newer, but another release train PR is already open; waiting for it to merge and deploy."
      printf '%s\n' "${pending_other_release_train_prs}"
      return 1
    fi
  elif [[ -n "${REQUESTED_TAG}" ]]; then
    echo "Bypassing prior release train gate for manual dispatch of ${REQUESTED_TAG}."
  fi

  if [[ -z "${REQUESTED_TAG}" ]]; then
    current_termux_tag="$(release_branch_current_termux_tag "${release_branch}")"
    if [[ -n "${current_termux_tag}" && "${current_termux_tag}" != "${termux_tag}" ]]; then
      if ! gh release view "${current_termux_tag}" --repo "${GITHUB_REPOSITORY}" >/dev/null 2>&1; then
        echo "${upstream_tag} is newer, but ${release_branch} is still waiting for ${current_termux_tag} to be deployed."
        return 1
      fi
    fi
  fi

  emit_selected_release "codex" \
    "${release_json}" \
    "${release_train}" \
    "${release_branch}" \
    "${work_branch}" \
    "${termux_tag}"
  return 0
}

maybe_select_release() {
  local release_json="$1"
  local upstream_tag

  upstream_tag="$(jq -r '.tagName // empty' <<< "${release_json}")"
  case "${upstream_tag}" in
    rust-v*)
      maybe_select_codex_release "${release_json}"
      ;;
    rusty-v8-v*)
      maybe_select_rusty_v8_release "${release_json}"
      ;;
    *)
      echo "Skipping unsupported upstream release tag: ${upstream_tag}"
      return 1
      ;;
  esac
}

if [[ -n "${REQUESTED_TAG}" ]]; then
  release_json="$(release_json_for_tag "${REQUESTED_TAG}")"
  if maybe_select_release "${release_json}"; then
    exit 0
  fi
else
  while IFS= read -r release_json; do
    upstream_tag="$(jq -r '.tagName // empty' <<< "${release_json}")"
    case "${upstream_tag}" in
      rust-v* | rusty-v8-v*)
        if maybe_select_release "${release_json}"; then
          exit 0
        fi
        ;;
      *)
        echo "Skipping unsupported upstream release tag: ${upstream_tag}"
        ;;
    esac
  done < <(scheduled_upstream_releases_json | jq -c '.[]')
fi

echo "No upstream Codex or rusty_v8 release needs a Termux mirror."
echo "selected=false" >> "$GITHUB_OUTPUT"
append_selection_summary "false"
