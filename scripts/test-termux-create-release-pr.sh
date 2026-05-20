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

origin_ahead="${tmp_dir}/origin-ahead.git"
work_ahead="${tmp_dir}/work-ahead"
runner_temp_ahead="${tmp_dir}/runner-ahead"
github_output_ahead="${tmp_dir}/github-output-ahead"

mkdir -p "${runner_temp_ahead}"
git init --bare "${origin_ahead}" >/dev/null
git init "${work_ahead}" >/dev/null
cd "${work_ahead}"
git config user.name "Termux Release Test"
git config user.email "termux-release-test@example.invalid"

mkdir -p codex-rs src
cat > codex-rs/Cargo.toml <<'TOML'
[workspace.package]
version = "1.0.0"
TOML
printf 'release\n' > src/release.txt
git add codex-rs/Cargo.toml src/release.txt
git commit -m "upstream release" >/dev/null
git tag rust-v1.0.0
git branch -M main
git remote add origin "${origin_ahead}"
git push origin main rust-v1.0.0 >/dev/null

printf 'main-ahead-one\n' > src/main-ahead-one.txt
printf 'main-ahead-two\n' > src/main-ahead-two.txt
git add src/main-ahead-one.txt src/main-ahead-two.txt
git commit -m "upstream main after release" >/dev/null
git push origin main >/dev/null

git checkout -B wallentx/termux-target main >/dev/null
mkdir -p termux
printf 'compat\n' > termux/compat.txt
git add termux/compat.txt
git commit -m "termux compatibility changes" >/dev/null
git push origin wallentx/termux-target >/dev/null

git checkout -B automation main >/dev/null

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
RUNNER_TEMP="${runner_temp_ahead}" \
GITHUB_OUTPUT="${github_output_ahead}" \
GH_TOKEN="test-token" \
TERMUX_RELEASE_MAX_PATCH_FILES="1" \
bash "${script}" > "${tmp_dir}/stdout-ahead" 2> "${tmp_dir}/stderr-ahead" || {
  cat "${tmp_dir}/stdout-ahead" >&2
  cat "${tmp_dir}/stderr-ahead" >&2
  fail "patch branch synced with main was rejected"
}

git fetch origin release-train/1.0.0 >/dev/null
if ! git cat-file -e origin/release-train/1.0.0:termux/compat.txt 2>/dev/null; then
  fail "Termux compatibility file was not applied to release train"
fi
if git cat-file -e origin/release-train/1.0.0:src/main-ahead-one.txt 2>/dev/null; then
  fail "main-only file leaked into release train patch"
fi

echo "ok - patch branch synced with main contributes only Termux delta"

origin_conflict="${tmp_dir}/origin-conflict.git"
work_conflict="${tmp_dir}/work-conflict"
runner_temp_conflict="${tmp_dir}/runner-conflict"
github_output_conflict="${tmp_dir}/github-output-conflict"

mkdir -p "${runner_temp_conflict}"
git init --bare "${origin_conflict}" >/dev/null
git init "${work_conflict}" >/dev/null
cd "${work_conflict}"
git config user.name "Termux Release Test"
git config user.email "termux-release-test@example.invalid"

mkdir -p codex-rs src
cat > codex-rs/Cargo.toml <<'TOML'
[workspace.package]
version = "1.0.0"
TOML
printf 'release\n' > src/shared.txt
git add codex-rs/Cargo.toml src/shared.txt
git commit -m "upstream release" >/dev/null
git tag rust-v1.0.0
git branch -M main
git remote add origin "${origin_conflict}"
git push origin main rust-v1.0.0 >/dev/null

printf 'main\n' > src/shared.txt
printf 'main-ahead\n' > src/main-ahead.txt
git add src/shared.txt src/main-ahead.txt
git commit -m "upstream main after release" >/dev/null
git push origin main >/dev/null

git checkout -B wallentx/termux-target main >/dev/null
printf 'termux\n' > src/shared.txt
git add src/shared.txt
git commit -m "termux compatibility changes" >/dev/null
git push origin wallentx/termux-target >/dev/null

git checkout -B automation main >/dev/null

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
RUNNER_TEMP="${runner_temp_conflict}" \
GITHUB_OUTPUT="${github_output_conflict}" \
GH_TOKEN="test-token" \
TERMUX_RELEASE_MAX_PATCH_FILES="1" \
bash "${script}" > "${tmp_dir}/stdout-conflict" 2> "${tmp_dir}/stderr-conflict" || {
  cat "${tmp_dir}/stdout-conflict" >&2
  cat "${tmp_dir}/stderr-conflict" >&2
  fail "conflicting patch branch synced with main was rejected"
}

git fetch origin release-train/1.0.0 >/dev/null
if [[ "$(git show origin/release-train/1.0.0:src/shared.txt)" != "termux" ]]; then
  fail "conflict fallback did not keep the Termux-side file"
fi
if git cat-file -e origin/release-train/1.0.0:src/main-ahead.txt 2>/dev/null; then
  fail "main-only file leaked into conflict fallback release train"
fi

echo "ok - conflicting patch branch synced with main falls back to small PR"
