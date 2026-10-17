#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/termux-lock-audit.sh [--strict]

Audits Rust advisory file-lock usage that may need Termux compatibility handling.

By default this script prints findings and exits successfully. With --strict, it
exits non-zero when candidate file-lock calls are found in files that do not also
mention Unsupported/TryLockError handling.
USAGE
}

strict=false
for arg in "$@"; do
  case "$arg" in
    --strict)
      strict=true
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v rg >/dev/null 2>&1; then
  echo "error: rg is required for this audit" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

unsupported_pattern='std::fs::TryLockError|fs::TryLockError|TryLockError::|ErrorKind::Unsupported|std::io::ErrorKind::Unsupported'
lock_context_pattern='(\.lock\(\)|\.try_lock\(|\.try_lock_shared\(|TryLockError|ErrorKind::Unsupported)'
candidate_file_lock_pattern='\b([A-Za-z_][A-Za-z0-9_]*_file|file)\.(lock|try_lock|try_lock_shared)\('
candidate_false_positive_pattern='poisoned'
helper_pattern='codex_utils_file_lock|FileLockOutcome|TryFileLockOutcome|lock_exclusive_optional|try_lock_exclusive_optional|try_lock_shared_optional'

print_section() {
  printf '\n== %s ==\n' "$1"
}

print_section "Unsupported-aware lock handling"
echo "These files already mention Unsupported/TryLockError and are likely patched or intentionally reviewed:"

unsupported_count=0
while IFS= read -r file; do
  if rg -q "$lock_context_pattern" "$file"; then
    rg -n -H "$lock_context_pattern" "$file"
    unsupported_count=$((unsupported_count + 1))
  fi
done < <(rg -l "$unsupported_pattern" codex-rs -g '*.rs' || true)

if [ "$unsupported_count" -eq 0 ]; then
  echo "No unsupported-aware lock handling found."
fi

print_section "Optional file-lock helper usage"
echo "These files use the shared optional advisory file-lock helper:"

helper_count=0
while IFS= read -r file; do
  rg -n -H "$helper_pattern" "$file"
  helper_count=$((helper_count + 1))
done < <(rg -l "$helper_pattern" codex-rs -g '*.rs' || true)

if [ "$helper_count" -eq 0 ]; then
  echo "No optional file-lock helper usage found."
fi

print_section "Candidate file-lock calls for manual review"
echo "These receiver names may be std::fs::File advisory locks, but the file does not mention Unsupported/TryLockError handling."
echo "This section can include false positives, such as mutexes wrapping a file."

review_count=0
while IFS= read -r file; do
  if ! rg -q "$unsupported_pattern" "$file"; then
    matches="$(rg -n -H "$candidate_file_lock_pattern" "$file" | rg -v "$candidate_false_positive_pattern" || true)"
    if [ -n "$matches" ]; then
      echo "$matches"
      review_count=$((review_count + 1))
    fi
  fi
done < <(rg -l "$candidate_file_lock_pattern" codex-rs -g '*.rs' || true)

if [ "$review_count" -eq 0 ]; then
  echo "No unpatched candidate file-lock calls found."
fi

print_section "Candidate file-lock calls already in unsupported-aware files"
echo "These are candidate file-lock calls in files that already mention Unsupported/TryLockError handling:"

aware_candidate_count=0
while IFS= read -r file; do
  if rg -q "$unsupported_pattern" "$file"; then
    matches="$(rg -n -H "$candidate_file_lock_pattern" "$file" | rg -v "$candidate_false_positive_pattern" || true)"
    if [ -n "$matches" ]; then
      echo "$matches"
      aware_candidate_count=$((aware_candidate_count + 1))
    fi
  fi
done < <(rg -l "$candidate_file_lock_pattern" codex-rs -g '*.rs' || true)

if [ "$aware_candidate_count" -eq 0 ]; then
  echo "No unsupported-aware candidate file-lock calls found."
fi

print_section "Summary"
echo "unsupported-aware files: $unsupported_count"
echo "optional helper files: $helper_count"
echo "review candidate files: $review_count"
echo "unsupported-aware candidate files: $aware_candidate_count"

if [ "$strict" = true ] && [ "$review_count" -gt 0 ]; then
  echo "strict mode: manual-review candidate files were found" >&2
  exit 1
fi
