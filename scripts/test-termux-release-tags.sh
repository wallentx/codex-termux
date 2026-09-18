#!/usr/bin/env bash
# Exercise real compatibility code, not a replacement patch or Cargo build.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/termux-release-paths.sh
source "${script_dir}/termux-release-paths.sh"
source_ref="${1:-rust-v0.155.0-alpha.9-termux}"
baseline="${2:-rust-v0.155.0-alpha.9}"
if (( $# >= 2 )); then shift 2; fi
if (( $# == 0 )); then
  set -- rust-v0.155.0 rust-v0.155.0-alpha.16 rust-v0.155.0-alpha.17
fi
for upstream in "$@"; do
  tree="$(python3 "${script_dir}/termux-release-tree.py" "${upstream}" "${source_ref}" "${baseline}" \
    "${TERMUX_RELEASE_BRANCH_SCRIPT_PATHS[@]}")"
  git diff --check "${upstream}" "${tree}"
  git diff --exit-code "${upstream}" "${tree}" -- .github codex-rs/config codex-rs/features
  git grep -q 'TermuxSelfUpdate' "${tree}" -- codex-rs/tui/src/update_action.rs
  git grep -q 'code-mode-host' "${tree}" -- codex-rs/tui/src/termux_update.rs
  if git grep -q 'Daemon(DaemonUpdateSource)' "${upstream}" -- codex-rs/tui/src/update_action.rs; then
    git grep -q 'Daemon(DaemonUpdateSource)' "${tree}" -- codex-rs/tui/src/update_action.rs
    git grep -q 'run_update_action(action, cli_executable).await?' "${tree}" -- codex-rs/cli/src/main.rs
    git grep -q 'handle_app_exit(exit_info, daemon_cli_executable.as_deref()).await?' "${tree}" -- codex-rs/cli/src/main.rs
  fi
  echo "ok - real ${upstream} preserves Termux updater and exact upstream config/features"
done
