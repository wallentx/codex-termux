#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/termux-create-release-pr.sh"
tmp_dir="$(mktemp -d)"
release_code_patch="${tmp_dir}/termux-release-code.patch"

cat > "${release_code_patch}" <<'PATCH'
diff --git a/codex-rs/cli/src/main.rs b/codex-rs/cli/src/main.rs
--- a/codex-rs/cli/src/main.rs
+++ b/codex-rs/cli/src/main.rs
@@ -1 +1 @@
-upstream release code
+termux release code
PATCH

cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

fail() {
  echo "not ok - $*" >&2
  exit 1
}

assert_ref_has_file() {
  local ref="$1"
  local path="$2"

  if ! git cat-file -e "${ref}:${path}" 2>/dev/null; then
    fail "${ref} does not contain ${path}"
  fi
}

assert_ref_lacks_file() {
  local ref="$1"
  local path="$2"

  if git cat-file -e "${ref}:${path}" 2>/dev/null; then
    fail "${ref} unexpectedly contains ${path}"
  fi
}

assert_ref_file_equals() {
  local ref="$1"
  local path="$2"
  local expected="$3"
  local actual

  actual="$(git show "${ref}:${path}")"
  if [[ "${actual}" != "${expected}" ]]; then
    printf 'expected %s:%s to equal:\n%s\nactual:\n%s\n' "${ref}" "${path}" "${expected}" "${actual}" >&2
    fail "${ref}:${path} did not match expected content"
  fi
}

assert_ref_is_ancestor() {
  local ancestor="$1"
  local descendant="$2"

  if ! git merge-base --is-ancestor "${ancestor}" "${descendant}"; then
    fail "${ancestor} is not an ancestor of ${descendant}"
  fi
}

run_release_pr_script() {
  local runner_temp="$1"
  local github_output="$2"
  local upstream_tag="${3:-rust-v1.0.0}"
  local termux_tag="${4:-${upstream_tag}-termux}"

  PATH="${bin_dir}:${PATH}" \
  GITHUB_REPOSITORY="wallentx/codex-termux" \
  UPSTREAM_REPO="openai/codex" \
  UPSTREAM_TAG="${upstream_tag}" \
  UPSTREAM_NAME="Codex ${upstream_tag}" \
  UPSTREAM_HTML_URL="https://github.com/openai/codex/releases/tag/${upstream_tag}" \
  UPSTREAM_PRERELEASE="false" \
  UPSTREAM_TARGET="$(git rev-parse "${upstream_tag}")" \
  UPSTREAM_ID="1" \
  UPSTREAM_BODY="Test upstream release" \
  RELEASE_TRAIN="1.0.0" \
  RELEASE_BRANCH="release/1.0.0" \
  WORK_BRANCH="release-train/1.0.0" \
  TERMUX_TAG="${termux_tag}" \
  PATCH_BRANCH="wallentx/termux-target" \
  REVIEWER="wallentx" \
  RUNNER_TEMP="${runner_temp}" \
  GITHUB_OUTPUT="${github_output}" \
  TERMUX_RELEASE_CODE_PATCH="${release_code_patch}" \
  GH_TOKEN="test-token" \
  bash "${script}"
}

bin_dir="${tmp_dir}/bin"
mkdir -p "${bin_dir}"

cat > "${bin_dir}/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-} ${2:-}" in
  "pr list")
    if [[ -n "${TERMUX_TEST_OPEN_PR_TAG:-}" ]]; then
      cat <<JSON
[{"number":7,"title":"Termux ${TERMUX_TEST_OPEN_PR_TAG}","body":"- Upstream tag: \`${TERMUX_TEST_OPEN_PR_TAG}\`\n- Release train branch: \`release/1.0.0\`","headRefName":"${TERMUX_TEST_OPEN_PR_HEAD:-release-train/1.0.0}","baseRefName":"release/1.0.0","url":"https://github.com/wallentx/codex-termux/pull/7","state":"OPEN","isDraft":false,"mergedAt":null}]
JSON
    else
      printf '[]\n'
    fi
    ;;
  "pr create")
    printf 'https://github.com/wallentx/codex-termux/pull/1\n'
    ;;
  "pr view")
    cat <<'JSON'
{"headRefOid":"test-head-sha","mergeStateStatus":"CLEAN","mergeable":"MERGEABLE","state":"OPEN","url":"https://github.com/wallentx/codex-termux/pull/1"}
JSON
    ;;
  "pr merge")
    expected=(
      "pr" "merge" "${3:-}"
      "--repo" "wallentx/codex-termux"
      "--squash"
      "--auto"
      "--delete-branch"
      "--match-head-commit" "test-head-sha"
    )
    if [[ "$*" != "${expected[*]}" ]]; then
      echo "unexpected gh pr merge invocation: $*" >&2
      exit 1
    fi
    ;;
  "pr edit"|"label create")
    ;;
  "release view")
    exit 1
    ;;
  *)
    echo "unexpected gh invocation: $*" >&2
    exit 1
    ;;
esac
STUB
chmod +x "${bin_dir}/gh"

origin="${tmp_dir}/origin.git"
work="${tmp_dir}/work"
runner_temp="${tmp_dir}/runner"
github_output="${tmp_dir}/github-output"

mkdir -p "${runner_temp}"
git init --bare "${origin}" >/dev/null
git init "${work}" >/dev/null
cd "${work}"
git config user.name "Termux Release Test"
git config user.email "termux-release-test@example.invalid"

mkdir -p .github/workflows codex-rs/cli/src codex-rs/tui/src src
printf 'base workflow\n' > .github/workflows/rust-release.yml
cat > codex-rs/Cargo.toml <<'TOML'
[workspace.package]
version = "1.0.0"
TOML
printf 'upstream release code\n' > codex-rs/cli/src/main.rs
for path in lib.rs termux_update.rs update_action.rs update_prompt.rs update_versions.rs updates.rs; do
  printf 'fixture\n' > "codex-rs/tui/src/${path}"
done
printf 'release\n' > src/shared.txt
git add .github/workflows/rust-release.yml codex-rs src/shared.txt
git commit -m "upstream release" >/dev/null
git tag rust-v1.0.0
git branch -M main
git remote add origin "${origin}"
git push origin main rust-v1.0.0 >/dev/null

printf 'upstream-after-tag\n' > src/upstream-after-tag.txt
git add src/upstream-after-tag.txt
git commit -m "upstream after release tag" >/dev/null
git push origin main >/dev/null

git checkout -B wallentx/termux-target main >/dev/null
mkdir -p termux
perl -0pi -e 's/version = "1\.0\.0"/version = "1.0.0-alpha.target"/' codex-rs/Cargo.toml
printf 'compat\n' > termux/compat.txt
printf 'termux\n' > src/shared.txt
printf 'termux release code with target drift\n' > codex-rs/cli/src/main.rs
git add codex-rs/Cargo.toml codex-rs/cli/src/main.rs termux/compat.txt src/shared.txt
git commit -m "termux compatibility changes" >/dev/null
git push origin wallentx/termux-target >/dev/null

git checkout -B release-train/1.0.0 rust-v1.0.0 >/dev/null
printf 'stale\n' > stale-work-branch.txt
git add stale-work-branch.txt
git commit -m "stale release train branch" >/dev/null
git push origin release-train/1.0.0 >/dev/null

git checkout -B automation main >/dev/null

run_release_pr_script "${runner_temp}" "${github_output}" > "${tmp_dir}/stdout" 2> "${tmp_dir}/stderr" || {
  cat "${tmp_dir}/stdout" >&2
  cat "${tmp_dir}/stderr" >&2
  fail "release PR script rejected a target branch that differs from the release tag"
}

git fetch origin release/1.0.0 release-train/1.0.0 >/dev/null

assert_ref_file_equals origin/release/1.0.0 src/shared.txt "release"
assert_ref_file_equals origin/release/1.0.0 codex-rs/cli/src/main.rs "termux release code"
assert_ref_lacks_file origin/release/1.0.0 src/upstream-after-tag.txt
assert_ref_lacks_file origin/release/1.0.0 termux/compat.txt
assert_ref_has_file origin/release/1.0.0 scripts/termux-download-release-artifact.sh

assert_ref_is_ancestor origin/release/1.0.0 origin/release-train/1.0.0
assert_ref_file_equals origin/release-train/1.0.0 src/shared.txt "termux"
assert_ref_file_equals origin/release-train/1.0.0 codex-rs/cli/src/main.rs "termux release code"
assert_ref_file_equals origin/release-train/1.0.0 codex-rs/Cargo.toml "[workspace.package]
version = \"1.0.0\""
assert_ref_has_file origin/release-train/1.0.0 src/upstream-after-tag.txt
assert_ref_has_file origin/release-train/1.0.0 termux/compat.txt
assert_ref_has_file origin/release-train/1.0.0 .github/termux-release.json
assert_ref_has_file origin/release-train/1.0.0 scripts/termux-download-release-artifact.sh
assert_ref_lacks_file origin/release-train/1.0.0 stale-work-branch.txt

if [[ "$(cat "${github_output}")" != "pr_url=https://github.com/wallentx/codex-termux/pull/1" ]]; then
  cat "${github_output}" >&2
  fail "release PR URL was not written to GITHUB_OUTPUT"
fi

echo "ok - release PR head merges the release base and preserves Termux target changes"

origin_update="${tmp_dir}/origin-update.git"
work_update="${tmp_dir}/work-update"
runner_temp_update="${tmp_dir}/runner-update"
github_output_update="${tmp_dir}/github-output-update"

mkdir -p "${runner_temp_update}"
git init --bare "${origin_update}" >/dev/null
git init "${work_update}" >/dev/null
cd "${work_update}"
git config user.name "Termux Release Test"
git config user.email "termux-release-test@example.invalid"

mkdir -p .github/workflows codex-rs/cli/src codex-rs/tui/src src
printf 'base workflow\n' > .github/workflows/rust-release.yml
cat > codex-rs/Cargo.toml <<'TOML'
[workspace.package]
version = "1.0.0-alpha.1"

[workspace.dependencies]
base = "1"

[workspace.metadata.fixture]
release = "old"
TOML
printf 'upstream release code\n' > codex-rs/cli/src/main.rs
for path in lib.rs termux_update.rs update_action.rs update_prompt.rs update_versions.rs updates.rs; do
  printf 'fixture\n' > "codex-rs/tui/src/${path}"
done
printf 'old-release\n' > src/shared.txt
git add .github/workflows/rust-release.yml codex-rs src/shared.txt
git commit -m "old upstream release" >/dev/null
git tag rust-v1.0.0-alpha.1
git branch -M main
git remote add origin "${origin_update}"
git push origin main rust-v1.0.0-alpha.1 >/dev/null

git checkout -B release/1.0.0 rust-v1.0.0-alpha.1 >/dev/null
printf 'old-base\n' > stale-release-branch.txt
git add stale-release-branch.txt
git commit -m "old release branch" >/dev/null
git push origin release/1.0.0 >/dev/null

git checkout main >/dev/null
perl -0pi -e 's/version = "1\.0\.0-alpha\.1"/version = "1.0.0-alpha.2"/' codex-rs/Cargo.toml
perl -0pi -e 's/release = "old"/release = "new"/' codex-rs/Cargo.toml
printf 'new-release\n' > src/shared.txt
printf 'new-tag\n' > src/new-tag.txt
git add codex-rs/Cargo.toml src/shared.txt src/new-tag.txt
git commit -m "new upstream release" >/dev/null
git tag rust-v1.0.0-alpha.2
git push origin main rust-v1.0.0-alpha.2 >/dev/null

git checkout -B wallentx/termux-target rust-v1.0.0-alpha.1 >/dev/null
mkdir -p termux
perl -0pi -e 's/version = "1\.0\.0-alpha\.1"/version = "1.0.0-alpha.target"/' codex-rs/Cargo.toml
perl -0pi -e 's/base = "1"/base = "1"\ntarget-only = "1"/' codex-rs/Cargo.toml
printf 'termux workflow\n' > .github/workflows/rust-release.yml
printf 'compat\n' > termux/compat.txt
printf 'termux release code with target drift\n' > codex-rs/cli/src/main.rs
git add .github/workflows/rust-release.yml codex-rs/Cargo.toml codex-rs/cli/src/main.rs termux/compat.txt
git commit -m "termux compatibility changes" >/dev/null
git push origin wallentx/termux-target >/dev/null

git checkout -B release-train/1.0.0 rust-v1.0.0-alpha.1 >/dev/null
printf 'stale\n' > stale-work-branch.txt
git add stale-work-branch.txt
git commit -m "old open release train branch" >/dev/null
git push origin release-train/1.0.0 >/dev/null

git checkout -B automation main >/dev/null

TERMUX_TEST_OPEN_PR_TAG="rust-v1.0.0-alpha.1" \
run_release_pr_script \
  "${runner_temp_update}" \
  "${github_output_update}" \
  "rust-v1.0.0-alpha.2" \
  "rust-v1.0.0-alpha.2-termux" \
  > "${tmp_dir}/stdout-update" 2> "${tmp_dir}/stderr-update" || {
    cat "${tmp_dir}/stdout-update" >&2
    cat "${tmp_dir}/stderr-update" >&2
    fail "release PR script failed to rebuild an older open release train"
  }

git fetch origin release/1.0.0 release-train/1.0.0 >/dev/null

assert_ref_file_equals origin/release/1.0.0 src/shared.txt "new-release"
assert_ref_file_equals origin/release/1.0.0 codex-rs/cli/src/main.rs "termux release code"
assert_ref_file_equals origin/release/1.0.0 codex-rs/Cargo.toml "[workspace.package]
version = \"1.0.0-alpha.2\"

[workspace.dependencies]
base = \"1\"

[workspace.metadata.fixture]
release = \"new\""
assert_ref_lacks_file origin/release/1.0.0 stale-release-branch.txt

assert_ref_is_ancestor origin/release/1.0.0 origin/release-train/1.0.0
assert_ref_file_equals origin/release-train/1.0.0 src/shared.txt "new-release"
assert_ref_file_equals origin/release-train/1.0.0 codex-rs/cli/src/main.rs "termux release code"
assert_ref_file_equals origin/release-train/1.0.0 codex-rs/Cargo.toml "[workspace.package]
version = \"1.0.0-alpha.2\"

[workspace.dependencies]
base = \"1\"
target-only = \"1\"

[workspace.metadata.fixture]
release = \"new\""
assert_ref_file_equals \
  origin/release-train/1.0.0 \
  .github/workflows/rust-release.yml \
  "$(cat "${repo_root}/.github/workflows/rust-release.yml")"
assert_ref_has_file origin/release-train/1.0.0 termux/compat.txt
assert_ref_lacks_file origin/release-train/1.0.0 stale-work-branch.txt

if [[ "$(cat "${github_output_update}")" != "pr_url=https://github.com/wallentx/codex-termux/pull/7" ]]; then
  cat "${github_output_update}" >&2
  fail "existing release PR URL was not written to GITHUB_OUTPUT"
fi

echo "ok - newer upstream tag rebuilds and merges the release base into the open train"
