#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/termux-create-release-pr.sh"
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
runner_temp="${tmp_dir}/runner"
github_output="${tmp_dir}/github-output"

mkdir -p "${bin_dir}" "${runner_temp}"

cat > "${bin_dir}/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-} ${2:-}" in
  "pr list")
    printf '[]\n'
    ;;
  "pr create")
    printf 'https://github.com/wallentx/codex-termux/pull/1\n'
    ;;
  "pr edit"|"label create")
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

mkdir -p codex-rs src
cat > codex-rs/Cargo.toml <<'TOML'
[workspace.package]
version = "1.0.0"
TOML
printf 'upstream\n' > src/upstream.txt
git add codex-rs/Cargo.toml src/upstream.txt
git commit -m "upstream release" >/dev/null
git tag rust-v1.0.0
git branch -M main
git remote add origin "${origin}"
git push origin main rust-v1.0.0 >/dev/null

git checkout -B wallentx/termux-target rust-v1.0.0 >/dev/null
mkdir -p termux
printf 'one\n' > termux/one.txt
printf 'two\n' > termux/two.txt
git add termux/one.txt termux/two.txt
git commit -m "termux compatibility changes" >/dev/null
git push origin wallentx/termux-target >/dev/null

git checkout -B automation main >/dev/null

set +e
PATH="${bin_dir}:${PATH}" \
GITHUB_REPOSITORY="wallentx/codex-termux" \
UPSTREAM_REPO="openai/codex" \
UPSTREAM_TAG="rust-v1.0.0" \
UPSTREAM_NAME="Codex 1.0.0" \
UPSTREAM_HTML_URL="https://github.com/openai/codex/releases/tag/rust-v1.0.0" \
UPSTREAM_PRERELEASE="false" \
UPSTREAM_TARGET="$(git rev-parse rust-v1.0.0)" \
UPSTREAM_ID="1" \
UPSTREAM_BODY="Test upstream release" \
RELEASE_TRAIN="1.0.0" \
RELEASE_BRANCH="release/1.0.0" \
WORK_BRANCH="release-train/1.0.0" \
TERMUX_TAG="rust-v1.0.0-termux" \
PATCH_BRANCH="wallentx/termux-target" \
REVIEWER="wallentx" \
RUNNER_TEMP="${runner_temp}" \
GITHUB_OUTPUT="${github_output}" \
GH_TOKEN="test-token" \
TERMUX_RELEASE_MAX_PATCH_FILES="1" \
bash "${script}" > "${tmp_dir}/stdout" 2> "${tmp_dir}/stderr"
status=$?
set -e

if [[ "${status}" -eq 0 ]]; then
  cat "${tmp_dir}/stdout" >&2
  cat "${tmp_dir}/stderr" >&2
  fail "oversized Termux patch was accepted"
fi

if [[ "$(cat "${tmp_dir}/stderr")" != *"Termux patch scope too large"* ]]; then
  cat "${tmp_dir}/stdout" >&2
  cat "${tmp_dir}/stderr" >&2
  fail "oversized Termux patch did not report the scope guard"
fi

echo "ok - oversized Termux patch is rejected"
