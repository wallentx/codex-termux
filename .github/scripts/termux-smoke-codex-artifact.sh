#!/usr/bin/env bash
set -Eeuo pipefail

archive="${1:?Usage: termux-smoke-codex-artifact.sh <artifact.tar.gz>}"
if [[ ! -f "$archive" ]]; then
    echo "Artifact archive not found: $archive" >&2
    exit 1
fi

export CODEX_SMOKE_ARCHIVE="$archive"

bash .github/scripts/run-termux-pacman.sh ca-certificates libc++ tar zstd <<'TERMUX_SCRIPT'
set -Eeuo pipefail

archive="${CODEX_SMOKE_ARCHIVE:?CODEX_SMOKE_ARCHIVE is required}"
if [[ ! -f "$archive" ]]; then
    echo "Artifact archive not found inside Termux workspace: $archive" >&2
    exit 1
fi

rm -rf codex-smoke
mkdir -p codex-smoke
tar -xzf "$archive" -C codex-smoke
find codex-smoke -maxdepth 3 -type f -print

codex=""
for candidate in \
    codex-smoke/codex \
    codex-smoke/bin/codex \
    codex-smoke/codex-aarch64-linux-android
do
    if [[ -x "$candidate" ]]; then
        codex="$candidate"
        break
    fi
done

if [[ -z "$codex" ]]; then
    echo "Could not find executable codex in $archive." >&2
    exit 1
fi

export CODEX_HOME="$PWD/codex-home"
export RUST_BACKTRACE=1
mkdir -p "$CODEX_HOME"

"$codex" --version
"$codex" --help | tee codex-help.txt
grep -Eiq 'codex|usage' codex-help.txt
"$codex" exec --help | tee codex-exec-help.txt
grep -Eiq 'exec|usage' codex-exec-help.txt
TERMUX_SCRIPT
