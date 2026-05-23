#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/termux-select-release.sh"
tmp_dir="$(mktemp -d)"

cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

fail() {
  echo "not ok - $*" >&2
  exit 1
}

origin="${tmp_dir}/origin.git"
work="${tmp_dir}/work"
bin_dir="${tmp_dir}/bin"
github_output="${tmp_dir}/github-output"

mkdir -p "${bin_dir}"

cat > "${bin_dir}/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-} ${2:-} ${3:-}" in
  "api --method GET")
    case "${4:-}" in
      "repos/openai/codex/releases?per_page=30")
        cat <<'JSON'
[
  {
    "tag_name": "rust-v1.0.0-alpha.1",
    "name": "Codex 1.0.0 alpha 1",
    "body": "Test upstream release",
    "html_url": "https://github.com/openai/codex/releases/tag/rust-v1.0.0-alpha.1",
    "prerelease": true,
    "target_commitish": "abc123",
    "id": 1
  }
]
JSON
        ;;
      "repos/openai/codex/releases/tags/rust-v1.0.0-alpha.1")
        cat <<'JSON'
{
  "tag_name": "rust-v1.0.0-alpha.1",
  "name": "Codex 1.0.0 alpha 1",
  "body": "Test upstream release",
  "html_url": "https://github.com/openai/codex/releases/tag/rust-v1.0.0-alpha.1",
  "prerelease": true,
  "target_commitish": "abc123",
  "id": 1
}
JSON
        ;;
      *)
        echo "unexpected gh api path: ${4:-}" >&2
        exit 1
        ;;
    esac
    ;;
  "release view rust-v1.0.0-alpha.1-termux")
    if [[ "${TERMUX_TEST_TERMUX_RELEASE_TAGS:-}" == *"rust-v1.0.0-alpha.1-termux"* ]]; then
      exit 0
    fi
    exit 1
    ;;
  "release view rust-v1.0.0-termux")
    if [[ "${TERMUX_TEST_TERMUX_RELEASE_TAGS:-}" == *"rust-v1.0.0-termux"* ]]; then
      exit 0
    fi
    exit 1
    ;;
  "release list --repo")
    if [[ -n "${TERMUX_TEST_TERMUX_RELEASE_TAGS:-}" ]]; then
      printf '%s\n' ${TERMUX_TEST_TERMUX_RELEASE_TAGS}
    fi
    ;;
  "pr list --repo")
    printf '[]\n'
    ;;
  *)
    echo "unexpected gh invocation: $*" >&2
    exit 1
    ;;
esac
STUB
chmod +x "${bin_dir}/gh"

git init --bare "${origin}" >/dev/null
git init "${work}" >/dev/null
cd "${work}"
git config user.name "Termux Release Test"
git config user.email "termux-release-test@example.invalid"

mkdir -p .github
cat > .github/termux-release.json <<'JSON'
{
  "upstream_tag": "rust-v1.0.0-alpha.1",
  "termux_tag": "rust-v1.0.0-alpha.1-termux"
}
JSON
git add .github/termux-release.json
git commit -m "release metadata" >/dev/null
git branch -M release/1.0.0
git remote add origin "${origin}"
git push origin release/1.0.0 >/dev/null
git fetch origin release/1.0.0 >/dev/null

PATH="${bin_dir}:${PATH}" \
GITHUB_REPOSITORY="wallentx/codex-termux" \
UPSTREAM_REPO="openai/codex" \
REQUESTED_TAG="rust-v1.0.0-alpha.1" \
BYPASS_PRIOR_RELEASE_TRAIN="true" \
GITHUB_OUTPUT="${github_output}" \
GH_TOKEN="test-token" \
bash "${script}" > "${tmp_dir}/stdout" 2> "${tmp_dir}/stderr"

if ! grep -qx 'selected=true' "${github_output}"; then
  cat "${tmp_dir}/stdout" >&2
  cat "${tmp_dir}/stderr" >&2
  cat "${github_output}" >&2
  fail "missing Termux release was not selected for recreation"
fi

echo "ok - missing Termux release is selected for recreation"

scheduled_output="${tmp_dir}/github-output-scheduled"
PATH="${bin_dir}:${PATH}" \
GITHUB_REPOSITORY="wallentx/codex-termux" \
UPSTREAM_REPO="openai/codex" \
REQUESTED_TAG="" \
BYPASS_PRIOR_RELEASE_TRAIN="false" \
GITHUB_OUTPUT="${scheduled_output}" \
GH_TOKEN="test-token" \
TERMUX_TEST_TERMUX_RELEASE_TAGS="rust-v1.0.0-alpha.1-termux" \
bash "${script}" > "${tmp_dir}/stdout-scheduled" 2> "${tmp_dir}/stderr-scheduled"

if ! grep -qx 'selected=false' "${scheduled_output}"; then
  cat "${tmp_dir}/stdout-scheduled" >&2
  cat "${tmp_dir}/stderr-scheduled" >&2
  cat "${scheduled_output}" >&2
  fail "scheduled prerelease Termux tag was not recognized as already mirrored"
fi

echo "ok - scheduled selection accepts prerelease Termux mirror tags"
