#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
script="${repo_root}/scripts/termux-create-checkpoint-pr.sh"
tmp_dir="$(mktemp -d)"

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

bin_dir="${tmp_dir}/bin"
origin="${tmp_dir}/origin.git"
work="${tmp_dir}/work"
runner_temp="${tmp_dir}/runner"
github_output="${tmp_dir}/github-output"

mkdir -p "${bin_dir}" "${runner_temp}"

cat > "${bin_dir}/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-} ${2:-}" in
  "pr list")
    ;;
  "pr view")
    printf '{"headRefOid":"checkpoint-head-sha","state":"OPEN","url":"%s"}\n' "${3:-}"
    ;;
  "pr merge")
    [[ "${4:-}" == "--repo" ]] || {
      echo "expected --repo as fourth pr merge arg: $*" >&2
      exit 1
    }
    [[ "$*" == *" --merge "* ]] || {
      echo "checkpoint auto-merge must use --merge: $*" >&2
      exit 1
    }
    [[ "$*" != *" --squash "* ]] || {
      echo "checkpoint auto-merge must not use --squash: $*" >&2
      exit 1
    }
    [[ "$*" == *" --auto "* ]] || {
      echo "expected --auto: $*" >&2
      exit 1
    }
    [[ "$*" == *" --delete-branch "* ]] || {
      echo "expected --delete-branch: $*" >&2
      exit 1
    }
    [[ "$*" == *" --match-head-commit checkpoint-head-sha"* ]] || {
      echo "expected --match-head-commit checkpoint-head-sha: $*" >&2
      exit 1
    }
    ;;
  "pr create")
    printf 'https://github.com/wallentx/codex-termux/pull/2\n'
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
git config user.name "Termux Checkpoint Test"
git config user.email "termux-checkpoint-test@example.invalid"

mkdir -p codex-rs/cli/src scripts src
printf 'target code\n' > codex-rs/cli/src/main.rs
printf 'target-helper\n' > scripts/termux-configure-git.sh
printf 'target\n' > src/app.txt
printf 'shared\n' > src/conflict.txt
git add codex-rs/cli/src/main.rs scripts/termux-configure-git.sh src/app.txt src/conflict.txt
git commit -m "target baseline" >/dev/null
git branch -M wallentx/termux-target
git remote add origin "${origin}"
git push origin wallentx/termux-target >/dev/null

git checkout -B release/1.0.0 wallentx/termux-target >/dev/null
mkdir -p .github scripts
git rm scripts/termux-configure-git.sh >/dev/null
mkdir -p scripts
printf '{"termux_tag":"rust-v1.0.0-termux"}\n' > .github/termux-release.json
printf 'tested release code\n' > codex-rs/cli/src/main.rs
printf 'release-helper\n' > scripts/termux-download-release-artifact.sh
printf 'release\n' > src/app.txt
printf 'release conflict\n' > src/conflict.txt
git add .github/termux-release.json codex-rs/cli/src/main.rs scripts/termux-download-release-artifact.sh src/app.txt src/conflict.txt
git commit -m "release branch state" >/dev/null
git push origin release/1.0.0 >/dev/null

git checkout wallentx/termux-target >/dev/null
printf 'target conflict\n' > src/conflict.txt
git add src/conflict.txt
git commit -m "target branch follow-up" >/dev/null
git push origin wallentx/termux-target >/dev/null

PATH="${bin_dir}:${PATH}" \
GITHUB_REPOSITORY="wallentx/codex-termux" \
DESTINATION_BRANCH="wallentx/termux-target" \
SOURCE_BRANCH="release/1.0.0" \
REVIEWER="wallentx" \
RUNNER_TEMP="${runner_temp}" \
GITHUB_OUTPUT="${github_output}" \
GH_TOKEN="test-token" \
bash "${script}" > "${tmp_dir}/stdout" 2> "${tmp_dir}/stderr" || {
  cat "${tmp_dir}/stdout" >&2
  cat "${tmp_dir}/stderr" >&2
  fail "checkpoint script failed"
}

git fetch origin checkpoint/wallentx_termux-target_from_release_1.0.0_$(git rev-parse --short=12 origin/release/1.0.0) >/dev/null
checkpoint_ref="origin/checkpoint/wallentx_termux-target_from_release_1.0.0_$(git rev-parse --short=12 origin/release/1.0.0)"

assert_ref_file_equals "${checkpoint_ref}" src/app.txt "release"
assert_ref_file_equals "${checkpoint_ref}" src/conflict.txt "target conflict"
assert_ref_file_equals "${checkpoint_ref}" codex-rs/cli/src/main.rs "tested release code"
assert_ref_file_equals "${checkpoint_ref}" scripts/termux-configure-git.sh "target-helper"
assert_ref_lacks_file "${checkpoint_ref}" scripts/termux-download-release-artifact.sh
assert_ref_lacks_file "${checkpoint_ref}" .github/termux-release.json

if ! git merge-base --is-ancestor origin/wallentx/termux-target "${checkpoint_ref}"; then
  fail "checkpoint did not retain the destination branch as an ancestor"
fi
if ! git merge-base --is-ancestor origin/release/1.0.0 "${checkpoint_ref}"; then
  fail "checkpoint did not retain the source branch as an ancestor"
fi

if [[ "$(cat "${github_output}")" != "pr_url=https://github.com/wallentx/codex-termux/pull/2" ]]; then
  cat "${github_output}" >&2
  fail "checkpoint PR URL was not written to GITHUB_OUTPUT"
fi

echo "ok - checkpoint carries tested code and keeps conflicted paths on the destination baseline"
