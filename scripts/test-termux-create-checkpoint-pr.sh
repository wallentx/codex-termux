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
merge_log="${tmp_dir}/merge-log"

mkdir -p "${bin_dir}" "${runner_temp}"

cat > "${bin_dir}/gh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-} ${2:-}" in
  "pr list")
    printf '%s' "${TERMUX_TEST_EXISTING_PR:-}"
    ;;
  "pr view")
    jq -n --arg body "${TERMUX_TEST_PR_BODY:-}" --arg url "${3:-}" \
      '{headRefOid:"checkpoint-head-sha",state:"OPEN",url:$url,body:$body,isDraft:false,mergeable:"MERGEABLE"}'
    ;;
  "pr merge")
    printf '%s\n' "$*" >> "${TERMUX_TEST_MERGE_LOG:?}"
    if [[ "$*" == *" --disable-auto" ]]; then
      exit 0
    fi
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

run_checkpoint() {
  PATH="${bin_dir}:${PATH}" \
  GITHUB_REPOSITORY="wallentx/codex-termux" \
  DESTINATION_BRANCH="wallentx/termux-target" \
  SOURCE_BRANCH="release/1.0.0" \
  REVIEWER="wallentx" \
  RUNNER_TEMP="${runner_temp}" \
  GITHUB_OUTPUT="${github_output}" \
  TERMUX_TEST_MERGE_LOG="${merge_log}" \
  GH_TOKEN="test-token" \
  bash "${script}"
}

git init --bare "${origin}" >/dev/null
git init "${work}" >/dev/null
cd "${work}"
git config user.name "Termux Checkpoint Test"
git config user.email "termux-checkpoint-test@example.invalid"

mkdir -p .github/workflows codex-rs/cli/src scripts src
printf 'shared workflow\n' > .github/workflows/test.yml
printf 'target code\n' > codex-rs/cli/src/main.rs
printf 'target-helper\n' > scripts/termux-configure-git.sh
printf 'target\n' > src/app.txt
printf 'shared\n' > src/conflict.txt
printf 'old writer API\n' > src/old-lock.rs
printf 'line %s\n' {1..20} > src/independent.txt
git add .github codex-rs/cli/src/main.rs scripts/termux-configure-git.sh src
git commit -m "common upstream ancestor" >/dev/null
common_ancestor="$(git rev-parse HEAD)"
printf 'target baseline\n' > src/conflict.txt
printf 'Termux lock fallback\n' > src/old-lock.rs
git add src/conflict.txt src/old-lock.rs
git commit -m "Termux patch-source baseline" >/dev/null
patch_source_sha="$(git rev-parse HEAD)"
git branch -M wallentx/termux-target
git remote add origin "${origin}"
git push origin wallentx/termux-target >/dev/null

git checkout -B release/1.0.0 "${common_ancestor}" >/dev/null
mkdir -p .github scripts
git rm scripts/termux-configure-git.sh >/dev/null
mkdir -p scripts
jq -n --arg sha "${patch_source_sha}" \
  '{termux_tag:"rust-v1.0.0-termux",patch_source_sha:$sha,patch_branch:"wallentx/termux-target"}' \
  > .github/termux-release.json
printf 'tested release code\n' > codex-rs/cli/src/main.rs
printf 'release-helper\n' > scripts/termux-download-release-artifact.sh
printf 'release\n' > src/app.txt
printf 'release conflict\n' > src/conflict.txt
printf 'release workflow\n' > .github/workflows/test.yml
git mv src/old-lock.rs src/new-lock.rs
printf 'new writer API with Termux fallback\n' > src/new-lock.rs
sed -i '1s/.*/release first line/' src/independent.txt
git add .github codex-rs/cli/src/main.rs scripts/termux-download-release-artifact.sh src
git commit -m "release branch state" >/dev/null
git push origin release/1.0.0 >/dev/null

git checkout wallentx/termux-target >/dev/null
printf 'independent target fix\n' > src/target-only.txt
printf 'destination workflow\n' > .github/workflows/test.yml
sed -i '$s/.*/target last line/' src/independent.txt
git add .github/workflows/test.yml src/target-only.txt src/independent.txt
git commit -m "target branch follow-up" >/dev/null
git push origin wallentx/termux-target >/dev/null

if git merge-tree --write-tree origin/wallentx/termux-target origin/release/1.0.0 \
  > "${tmp_dir}/ordinary-merge"; then
  fail "fixture must reproduce the ordinary merge's stale-ancestry conflict"
fi
run_checkpoint > "${tmp_dir}/stdout" 2> "${tmp_dir}/stderr" || {
  cat "${tmp_dir}/stdout" >&2
  cat "${tmp_dir}/stderr" >&2
  fail "checkpoint script failed"
}

git fetch origin "checkpoint/wallentx_termux-target_from_release_1.0.0_$(git rev-parse --short=12 origin/release/1.0.0)" >/dev/null
checkpoint_ref="origin/checkpoint/wallentx_termux-target_from_release_1.0.0_$(git rev-parse --short=12 origin/release/1.0.0)"

assert_ref_file_equals "${checkpoint_ref}" src/app.txt "release"
assert_ref_file_equals "${checkpoint_ref}" src/conflict.txt "release conflict"
assert_ref_file_equals "${checkpoint_ref}" src/target-only.txt "independent target fix"
assert_ref_file_equals "${checkpoint_ref}" src/new-lock.rs "new writer API with Termux fallback"
assert_ref_lacks_file "${checkpoint_ref}" src/old-lock.rs
assert_ref_file_equals "${checkpoint_ref}" .github/workflows/test.yml "destination workflow"
assert_ref_file_equals "${checkpoint_ref}" src/independent.txt \
  "$(printf 'release first line\n'; printf 'line %s\n' {2..19}; printf 'target last line\n')"
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

if [[ "$(wc -l < "${merge_log}")" -ne 1 ]]; then
  cat "${merge_log}" >&2
  fail "checkpoint PR auto-merge was not enabled after a clean snapshot-based merge"
fi

echo "ok - squashed release checkpoints cleanly and preserves independent target changes"

checkpoint_sha="$(git rev-parse "${checkpoint_ref}")"
git checkout wallentx/termux-target >/dev/null
printf 'genuine concurrent target edit\n' > src/conflict.txt
git add src/conflict.txt
git commit -m "concurrent target edit" >/dev/null
git push origin wallentx/termux-target >/dev/null
before_tree="$(git write-tree)"
if run_checkpoint > "${tmp_dir}/stdout-conflict" 2> "${tmp_dir}/stderr-conflict"; then
  fail "checkpoint accepted conflicting concurrent code edits"
fi
[[ "$(git write-tree)" == "${before_tree}" ]] || fail "conflict changed the caller index"
[[ "$(git rev-parse "${checkpoint_ref}")" == "${checkpoint_sha}" ]] || fail "conflict replaced published checkpoint"
[[ "$(wc -l < "${merge_log}")" -eq 1 ]] || fail "conflict enabled auto-merge"
grep -q 'src/conflict.txt' "${tmp_dir}/stderr-conflict" || fail "conflict path was not reported"
echo "ok - genuine concurrent edits stop without changing files, publishing, or enabling auto-merge"

if TERMUX_TEST_EXISTING_PR='{"state":"OPEN","url":"https://github.com/wallentx/codex-termux/pull/2"}' \
  TERMUX_TEST_PR_BODY='## Merge conflicts' \
  run_checkpoint > "${tmp_dir}/stdout-existing" 2> "${tmp_dir}/stderr-existing"; then
  fail "legacy conflicted checkpoint was allowed to auto-merge"
fi
[[ "$(tail -1 "${merge_log}")" == *" --disable-auto" ]] || fail "legacy checkpoint auto-merge was not disabled"
echo "ok - existing checkpoints that discarded conflicts have auto-merge disabled"

tree_script="${repo_root}/scripts/termux-checkpoint-tree.sh"
if RUNNER_TEMP="${runner_temp}" bash "${tree_script}" origin/release/1.0.0 HEAD other-target \
  > "${tmp_dir}/stdout-wrong-target" 2> "${tmp_dir}/stderr-wrong-target"; then
  fail "checkpoint accepted the wrong destination branch"
fi
if RUNNER_TEMP="${runner_temp}" bash "${tree_script}" "${common_ancestor}" HEAD wallentx/termux-target \
  > "${tmp_dir}/stdout-missing" 2> "${tmp_dir}/stderr-missing"; then
  fail "checkpoint accepted missing release metadata"
fi
if RUNNER_TEMP="${runner_temp}" bash "${tree_script}" origin/release/1.0.0 "${common_ancestor}" wallentx/termux-target \
  > "${tmp_dir}/stdout-ancestry" 2> "${tmp_dir}/stderr-ancestry"; then
  fail "checkpoint accepted a destination without the recorded patch-source ancestor"
fi
echo "ok - wrong destinations, missing metadata, and unrelated baselines fail closed"

result="$(RUNNER_TEMP="${runner_temp}" bash "${tree_script}" origin/release/1.0.0 "${checkpoint_sha}" wallentx/termux-target)"
[[ "${result}" == "$(git rev-parse "${checkpoint_sha}^{tree}")" ]] || fail "repeated checkpoint changed the code tree"
echo "ok - repeated checkpoint is idempotent"
