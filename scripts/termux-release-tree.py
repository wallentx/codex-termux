#!/usr/bin/env python3
"""Transplant a downstream delta onto an exact upstream tree without checkout."""

import io
import os
import posixpath
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path


def git(*args, data=None, env=None):
    return subprocess.check_output(["git", *args], input=data, env=env)


def paths_between(left, right):
    return set(
        filter(
            None,
            git("diff", "--no-renames", "--name-only", "-z", left, right).split(b"\0"),
        )
    )


def validate_manifests(tree):
    paths = set(git("ls-tree", "-r", "--name-only", tree).decode().splitlines())
    manifests = sorted(
        p for p in paths if p.startswith("codex-rs/") and p.endswith("Cargo.toml")
    )
    archive = git("archive", tree, "--", *manifests)
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        data = {
            p: tomllib.loads(files.extractfile(p).read().decode()) for p in manifests
        }
    workspace = data["codex-rs/Cargo.toml"]["workspace"]
    for member in workspace["members"]:
        if f"codex-rs/{member}/Cargo.toml" not in paths:
            raise ValueError(f"missing workspace member: {member}")
    for path, manifest in data.items():
        scopes = [manifest, *manifest.get("target", {}).values()]
        if path == "codex-rs/Cargo.toml":
            scopes.append(workspace)
        for scope in scopes:
            for kind in ("dependencies", "build-dependencies", "dev-dependencies"):
                for name, spec in scope.get(kind, {}).items():
                    if not isinstance(spec, dict):
                        continue
                    if spec.get("workspace") and name not in workspace.get(
                        "dependencies", {}
                    ):
                        raise ValueError(f"{path}: missing workspace dependency {name}")
                    if "path" in spec:
                        dep = posixpath.normpath(
                            f"{posixpath.dirname(path)}/{spec['path']}/Cargo.toml"
                        )
                        if dep not in paths:
                            raise ValueError(
                                f"{path}: missing dependency manifest {dep}"
                            )


def async_updater(text):
    """Keep upstream updater arguments while carrying the async Termux caller."""
    names = r"(?:handle_app_exit|run_update_action|run_update_command)"
    text = re.sub(rf"(?m)^fn ({names})\(", r"async fn \1(", text)
    # Upstream's call sites are single-line expressions. Match the whole call,
    # including nested arguments, rather than replacing its argument list.
    return re.sub(
        rf"(?m)^([ \t]*(?:return )?{names}\([^\n]*\))(\??;?[ \t]*)$",
        r"\1.await\2",
        text,
    )


def drop_retired_clipboard_guard(baseline, source, upstream):
    """Retire only the Android warning cleanup when its helper is gone."""
    if b"SuppressStderr" in upstream:
        return source
    expected = baseline
    for definition in (b"struct SuppressStderr;", b"impl SuppressStderr {"):
        original = b'#[cfg(not(target_os = "macos"))]\n' + definition
        replacement = (
            b'#[cfg(all(not(target_os = "android"), not(target_os = "macos")))]\n'
            + definition
        )
        if expected.count(original) != 1:
            return source
        expected = expected.replace(original, replacement, 1)
    # Compare the entire file so no functional downstream edit can be dropped.
    return baseline if source == expected else source


def merge_updater_additions(text, *, path=None):
    """Carry only reviewed Termux updater edits through upstream changes."""
    additions = {
        "    /// Update by replacing the current Termux binary from wallentx/codex-termux.\n    TermuxSelfUpdate,\n",
        '            UpdateAction::TermuxSelfUpdate => ("codex", &["update"]),\n',
        "        Some(UpdateAction::TermuxSelfUpdate) => termux_update::latest_release_version().await?,\n",
        "    if matches!(action, UpdateAction::TermuxSelfUpdate) {\n        run_termux_update().await?;\n        return Ok(());\n    }\n\n",
    }
    pattern = r"^<<<<<<<[^\n]*\n(.*?)^\|\|\|\|\|\|\|[^\n]*\n(.*?)^=======\n(.*?)^>>>>>>>[^\n]*\n"

    def resolve(match):
        ours, base, theirs = match.groups()
        if path == "codex-rs/tui/src/update_prompt.rs":
            declaration = "        let release_notes_url = self.update_action.release_notes_url();\n"
            if text.count(declaration) != 1:
                return match[0]
            constant = 'const RELEASE_NOTES_URL: &str = "https://github.com/openai/codex/releases/latest";\n\n'
            guard = "#[cfg(not(debug_assertions))]\n"
            if base == constant and not theirs and ours == constant + guard:
                return guard
            # Upstream redesigned the picker. The downstream delta in these
            # hunks changes only the URL expression, not layout or rendering.
            for original in (
                "                RELEASE_NOTES_URL.dim().underlined(),\n",
                "        crate::terminal_hyperlinks::mark_underlined_hyperlink(buf, area, RELEASE_NOTES_URL);\n",
            ):
                replacement = original.replace("RELEASE_NOTES_URL", "release_notes_url")
                if (
                    base.count(original) == 1
                    and theirs == base.replace(original, replacement, 1)
                    and ours.count(original) == 1
                ):
                    return ours.replace(original, replacement, 1)
            return match[0]
        test_header = "    fn standalone_update_commands_rerun_latest_installer() {\n"
        if path == "codex-rs/tui/src/update_action.rs" and base.startswith(test_header):
            assertion = (
                "        assert_eq!(\n"
                "            UpdateAction::TermuxSelfUpdate.command_args(),\n"
                '            ("codex", &["update"][..])\n'
                "        );\n"
            )
            # Upstream renamed this test while the downstream edit only prepends
            # an assertion, so keep the assertion in whichever test remains.
            if theirs == test_header + assertion + base[
                len(test_header) :
            ] and re.fullmatch(r"    fn \w+\(\) \{\n", ours):
                return ours + assertion
            return match[0]
        if not base and theirs in additions:
            return theirs + ours
        signature = (
            "async fn run_update_action(action: UpdateAction) -> anyhow::Result<()> {\n"
        )
        if (
            base == signature
            and theirs.startswith(signature)
            and theirs[len(signature) :] in additions
        ):
            current = re.match(
                r"async fn run_update_action\(\n    action: UpdateAction,\n    cli_executable: Option<&std::path::Path>,\n\) -> anyhow::Result<\(\)> \{\n",
                ours,
            )
            if current:
                return current[0] + theirs[len(signature) :] + ours[current.end() :]
        return match[0]

    return re.sub(pattern, resolve, text, flags=re.MULTILINE | re.DOTALL)


def merge_dependency_additions(text, *, path):
    """Preserve reviewed dependency additions without restoring upstream removals."""
    manifest = path.startswith("codex-rs/") and path.endswith("/Cargo.toml")
    if manifest:
        addition = re.compile(r"codex-utils-[a-z0-9-]+ = \{ workspace = true \}\n")
    elif path == "codex-rs/Cargo.lock":
        addition = re.compile(r' "codex-utils-[a-z0-9-]+",\n')
    else:
        return text
    pattern = r"^<<<<<<<[^\n]*\n(.*?)^\|\|\|\|\|\|\|[^\n]*\n(.*?)^=======\n(.*?)^>>>>>>>[^\n]*\n"

    def resolve(match):
        ours, base, theirs = match.groups()
        android_openssl = (
            "# Build OpenSSL from source for Android builds.\n"
            "[target.aarch64-linux-android.dependencies]\n"
            'openssl-sys = { workspace = true, features = ["vendored"] }\n\n'
        )
        # Git can group our standalone target section with a neighboring
        # section that upstream deleted. Retain only the downstream insertion.
        if (
            manifest
            and base
            and not ours
            and android_openssl not in base
            and theirs in (android_openssl + base, base + android_openssl)
        ):
            return android_openssl
        ours_lines = ours.splitlines(keepends=True)
        theirs_lines = theirs.splitlines(keepends=True)
        lines = ours_lines + theirs_lines
        if (
            base
            or not ours_lines
            or not theirs_lines
            or len(set(lines)) != len(lines)
            or not all(addition.fullmatch(line) for line in lines)
        ):
            return match[0]
        return "".join(sorted(lines))

    return re.sub(pattern, resolve, text, flags=re.MULTILINE | re.DOTALL)


def port_startup_lock_edits(upstream):
    import_anchor = "use codex_utils_absolute_path::AbsolutePathBuf;\n"
    field = "    _file: std::fs::File,\n"
    guard_field = "    _lock_dir_guard: Option<LockDirGuard>,\n"
    blocking_lock = """        let lock_dir_guard = match lock_exclusive_optional(&file)? {
            FileLockOutcome::Acquired => None,
            FileLockOutcome::Unsupported => {
                Some(acquire_sibling_lock_dir(startup_lock_path.as_path())?)
            }
        };
"""
    adapted = upstream
    for before, after in (
        (
            import_anchor,
            import_anchor
            + """use codex_utils_file_lock::FileLockOutcome;
use codex_utils_file_lock::LockDirGuard;
#[cfg(unix)]
use codex_utils_file_lock::TryFileLockOutcome;
#[cfg(unix)]
use codex_utils_file_lock::TryLockDirOutcome;
use codex_utils_file_lock::acquire_sibling_lock_dir;
use codex_utils_file_lock::lock_exclusive_optional;
#[cfg(unix)]
use codex_utils_file_lock::try_acquire_sibling_lock_dir;
#[cfg(unix)]
use codex_utils_file_lock::try_lock_exclusive_optional;
""",
        ),
        (field, field + guard_field),
        (
            "        file.lock()?;\n        Ok(AppServerStartupLock {\n            _file: file,\n",
            blocking_lock
            + "        Ok(AppServerStartupLock {\n            _file: file,\n"
            + "            _lock_dir_guard: lock_dir_guard,\n",
        ),
        (
            "    socket_guard._startup_lock._file.unlock()?;",
            """    let socket_guard = {
        let mut socket_guard = socket_guard;
        if socket_guard._startup_lock._lock_dir_guard.take().is_none() {
            socket_guard._startup_lock._file.unlock()?;
        }
        socket_guard
    };""",
        ),
        (
            """        match self._file.try_lock() {
            Ok(()) => {}
            Err(std::fs::TryLockError::WouldBlock) => return Ok(()),
            Err(std::fs::TryLockError::Error(err)) => return Err(err),
        }
""",
            """        let _cleanup_lock_dir_guard = if self._lock_dir_guard.is_some() {
            None
        } else {
            match try_lock_exclusive_optional(&self._file)? {
                TryFileLockOutcome::Acquired => None,
                TryFileLockOutcome::WouldBlock => return Ok(()),
                TryFileLockOutcome::Unsupported => {
                    match try_acquire_sibling_lock_dir(path.as_path())? {
                        TryLockDirOutcome::Acquired(guard) => Some(guard),
                        TryLockDirOutcome::WouldBlock => return Ok(()),
                    }
                }
            }
        };
""",
        ),
        (
            "    file.try_lock()?;\n",
            """    let lock_dir_guard = match try_lock_exclusive_optional(&file)? {
        TryFileLockOutcome::Acquired => None,
        TryFileLockOutcome::WouldBlock => return Err(ErrorKind::WouldBlock.into()),
        TryFileLockOutcome::Unsupported => {
            match try_acquire_sibling_lock_dir(startup_lock_path.as_path())? {
                TryLockDirOutcome::Acquired(guard) => Some(guard),
                TryLockDirOutcome::WouldBlock => return Err(ErrorKind::WouldBlock.into()),
            }
        }
    };
""",
        ),
        (
            "        _file: file,\n        removable_path: Some(startup_lock_path),",
            (
                "        _file: file,\n        _lock_dir_guard: lock_dir_guard,\n"
                "        removable_path: Some(startup_lock_path),"
            ),
        ),
    ):
        if adapted.count(before) != 1:
            raise ValueError(
                "unrecognized removable startup-lock code; refusing adaptation"
            )
        adapted = adapted.replace(before, after, 1)
    return adapted


def adapt_split_daemon_socket(baseline, source, upstream):
    """Carry the lock fallback to upstream's reusable private-socket binder."""
    split = "pub async fn bind_private_daemon_socket("
    if split not in upstream or split in baseline or source == baseline:
        return source
    if "use codex_utils_file_lock::" not in source:
        return source
    if port_startup_lock_edits(baseline) != source:
        raise ValueError("unrecognized Termux startup-lock delta; refusing adaptation")
    return port_startup_lock_edits(upstream)


def adapt_removable_startup_lock(baseline, source, upstream):
    """Carry the reviewed lock fallback through upstream's removable-lock redesign."""
    removable = "pub(super) fn try_acquire_removable_app_server_startup_lock("
    if (
        removable not in upstream
        or removable in baseline
        or source == baseline
        or "use codex_utils_file_lock::" not in source
    ):
        return source

    imports = """use codex_utils_file_lock::FileLockOutcome;
use codex_utils_file_lock::LockDirGuard;
use codex_utils_file_lock::acquire_sibling_lock_dir;
use codex_utils_file_lock::lock_exclusive_optional;
"""
    import_anchor = "use codex_utils_absolute_path::AbsolutePathBuf;\n"
    field = "    _file: std::fs::File,\n"
    guard_field = "    _lock_dir_guard: Option<LockDirGuard>,\n"
    blocking_lock = """        let lock_dir_guard = match lock_exclusive_optional(&file)? {
            FileLockOutcome::Acquired => None,
            FileLockOutcome::Unsupported => {
                Some(acquire_sibling_lock_dir(startup_lock_path.as_path())?)
            }
        };
"""
    expected = baseline
    for before, after in (
        (import_anchor, import_anchor + imports),
        (field, field + guard_field),
        (
            "        file.lock()?;\n        Ok(AppServerStartupLock { _file: file })\n",
            blocking_lock
            + """        Ok(AppServerStartupLock {
            _file: file,
            _lock_dir_guard: lock_dir_guard,
        })
""",
        ),
    ):
        if expected.count(before) != 1:
            raise ValueError("unrecognized startup-lock baseline; refusing adaptation")
        expected = expected.replace(before, after, 1)
    if source != expected:
        raise ValueError("unrecognized Termux startup-lock delta; refusing adaptation")

    return port_startup_lock_edits(upstream)


ARG0_PATH = "codex-rs/arg0/src/lib.rs"
DAEMON_PREPARE_PATH = "codex-rs/app-server-daemon/src/prepare_install.rs"

_RG_REQUIRED = """        if cfg!(windows) {
            "codex-path/rg.exe"
        } else {
            "codex-path/rg"
        },
    ];
"""
_RG_ANDROID_PATH = """    ];
    // Android uses the ripgrep installed by Termux, resolved through PATH.
    if !cfg!(target_os = "android") {
        names.push(if cfg!(windows) {
            "codex-path/rg.exe"
        } else {
            "codex-path/rg"
        });
    }
"""

# (before, after, occurrences) edits that make up the reviewed downstream deltas.
ARG0_LOCK_EDITS = (
    (
        "use codex_sandboxing::landlock::CODEX_LINUX_SANDBOX_ARG0;\n",
        (
            "use codex_sandboxing::landlock::CODEX_LINUX_SANDBOX_ARG0;\n"
            "use codex_utils_file_lock::TryFileLockOutcome;\n"
            "use codex_utils_file_lock::try_lock_exclusive_optional;\n"
        ),
        1,
    ),
    ("    _lock_file: File,\n", "    _lock_file: Option<File>,\n", 1),
    (
        "fn new(temp_dir: TempDir, lock_file: File, paths",
        "fn new(temp_dir: TempDir, lock_file: Option<File>, paths",
        1,
    ),
    (
        "\n    lock_file.try_lock()?;\n",
        """
    let lock_file = match try_lock_exclusive_optional(&lock_file)? {
        TryFileLockOutcome::Acquired => Some(lock_file),
        TryFileLockOutcome::Unsupported => None,
        TryFileLockOutcome::WouldBlock => {
            return Err(std::io::Error::from(std::io::ErrorKind::WouldBlock).into());
        }
    };
""",
        1,
    ),
    (
        """    match lock_file.try_lock() {
        Ok(()) => Ok(Some(lock_file)),
        Err(std::fs::TryLockError::WouldBlock) => Ok(None),
        Err(err) => Err(err.into()),
    }
""",
        """    match try_lock_exclusive_optional(&lock_file)? {
        TryFileLockOutcome::Acquired => Ok(Some(lock_file)),
        TryFileLockOutcome::WouldBlock | TryFileLockOutcome::Unsupported => Ok(None),
    }
""",
        1,
    ),
    (
        "            lock_file,\n            Arg0DispatchPaths {",
        "            Some(lock_file),\n            Arg0DispatchPaths {",
        2,
    ),
    (
        "        lock_file.try_lock()?;\n",
        """        match try_lock_exclusive_optional(&lock_file)? {
            TryFileLockOutcome::Acquired => {}
            TryFileLockOutcome::Unsupported => return Ok(()),
            TryFileLockOutcome::WouldBlock => {
                panic!("newly created lock file should not be locked");
            }
        }
""",
        1,
    ),
)

ANDROID_PACKAGE_EDITS = (
    (_RG_REQUIRED, _RG_ANDROID_PATH, 1),
    (
        '        ("macos", "aarch64") => Ok("aarch64-apple-darwin"),\n',
        (
            '        ("android", "aarch64") => Ok("aarch64-linux-android"),\n'
            '        ("macos", "aarch64") => Ok("aarch64-apple-darwin"),\n'
        ),
        1,
    ),
)


def port_exact_edits(baseline, source, upstream, edits, label):
    """Port a downstream delta made only of known edits onto a redesigned upstream file."""
    expected = baseline
    adapted = upstream
    for before, after, count in edits:
        if expected.count(before) != count or adapted.count(before) != count:
            raise ValueError(f"unrecognized {label} code; refusing adaptation")
        expected = expected.replace(before, after)
        adapted = adapted.replace(before, after)
    if source != expected:
        raise ValueError(f"unrecognized Termux {label} delta; refusing adaptation")
    return adapted


def adapt_arg0_lock(baseline, source, upstream):
    """Carry the unsupported-lock fallback through upstream's lock-before-directory drop order."""
    if ARG0_IMPORT in baseline and ARG0_IMPORT not in upstream:
        return adapt_arg0_sandbox_import(baseline, source, upstream)
    if (
        "    _lock_file: File,\n    _temp_dir: TempDir,\n" not in upstream
        or "    _lock_file: File,\n    _temp_dir: TempDir,\n" in baseline
        or source == baseline
        or "try_lock_exclusive_optional" not in source
    ):
        return source
    return port_exact_edits(baseline, source, upstream, ARG0_LOCK_EDITS, "arg0 lock")


WRITER_LOCK_PATH = "codex-rs/rollout/src/writer_lock.rs"
INSTALLATION_ID_PATH = "codex-rs/runtime-agent-instance/src/installation_id.rs"
_UNSUPPORTED_LOCK_BASE_IMPORTS = """use codex_protocol::ThreadId;
use codex_utils_file_lock::FileLockOutcome;
use codex_utils_file_lock::LockDirGuard;
use codex_utils_file_lock::TryFileLockOutcome;
use codex_utils_file_lock::TryLockDirOutcome;
use codex_utils_file_lock::acquire_sibling_lock_dir;
use codex_utils_file_lock::lock_exclusive_optional;
use codex_utils_file_lock::try_acquire_sibling_lock_dir;
use codex_utils_file_lock::try_lock_exclusive_optional;
"""
_UNSUPPORTED_LOCK_UPSTREAM_IMPORTS = """use codex_protocol::ThreadId;
use codex_utils_file_lock::LockDirGuard;
use codex_utils_file_lock::TryFileLockOutcome;
use codex_utils_file_lock::TryLockDirOutcome;
use codex_utils_file_lock::try_acquire_sibling_lock_dir;
use codex_utils_file_lock::try_lock_exclusive_optional;
use codex_utils_file_lock::try_lock_shared_optional;
"""

# Edits whose before text is identical in the paired baseline and the new upstream.
WRITER_LOCK_COMMON_EDITS = (
    (
        "    file: Option<File>,\n}\n",
        """    file: Option<File>,
    fallback_lock: Option<LockDirGuard>,
}

pub(crate) struct CoordinationLockGuard {
    _file: File,
    _fallback_lock: Option<LockDirGuard>,
}

enum TryWriterLockOutcome {
    Acquired(Option<LockDirGuard>),
    WouldBlock,
}
""",
        1,
    ),
    (
        """        match file.try_lock() {
            Ok(()) => {}
            Err(std::fs::TryLockError::WouldBlock) => {
                return Err(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    format!("thread {thread_id} already has an active writer"),
                ));
            }
            Err(std::fs::TryLockError::Error(err)) => {
                return Err(io::Error::other(format!(
                    "failed to acquire thread writer lock {}: {err}",
                    path.display()
                )));
            }
        }

        Ok(WriterLockGuard {
            coordinator: Arc::clone(self),
            path,
            file: Some(file),
        })
""",
        """        let fallback_lock = match try_lock_writer_file(&file, &path) {
            Ok(TryWriterLockOutcome::Acquired(fallback_lock)) => fallback_lock,
            Ok(TryWriterLockOutcome::WouldBlock) => {
                return Err(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    format!("thread {thread_id} already has an active writer"),
                ));
            }
            Err(err) => {
                return Err(io::Error::other(format!(
                    "failed to acquire thread writer lock {}: {err}",
                    path.display()
                )));
            }
        };

        Ok(WriterLockGuard {
            coordinator: Arc::clone(self),
            path,
            file: Some(file),
            fallback_lock,
        })
""",
        1,
    ),
    (
        """    ) -> io::Result<Option<File>> {
        let coordination_lock = self.lock_coordination()?;
        let path = self.directory.join(format!("{thread_id}.lock"));
        match OpenOptions::new().read(true).write(true).open(path) {
            Ok(file) => match file.try_lock() {
                Ok(()) => Ok(Some(coordination_lock)),
                Err(std::fs::TryLockError::WouldBlock) => Ok(None),
                Err(std::fs::TryLockError::Error(err)) => Err(err),
            },
""",
        """    ) -> io::Result<Option<CoordinationLockGuard>> {
        let coordination_lock = self.lock_coordination()?;
        let path = self.directory.join(format!("{thread_id}.lock"));
        match OpenOptions::new().read(true).write(true).open(&path) {
            Ok(file) => match try_lock_writer_file(&file, &path)? {
                TryWriterLockOutcome::Acquired(fallback_lock) => {
                    drop(fallback_lock);
                    Ok(Some(coordination_lock))
                }
                TryWriterLockOutcome::WouldBlock => Ok(None),
            },
""",
        1,
    ),
    (
        """            match file.try_lock() {
                Ok(()) => {
                    drop(file);
                    if let Err(err) = fs::remove_file(&path)
                        && err.kind() != io::ErrorKind::NotFound
                    {
                        warn!(
                            "failed to remove stale thread writer lock {}: {err}",
                            path.display()
                        );
                    }
                }
                Err(std::fs::TryLockError::WouldBlock) => {}
                Err(std::fs::TryLockError::Error(err)) => {
""",
        """            match try_lock_writer_file(&file, &path) {
                Ok(TryWriterLockOutcome::Acquired(fallback_lock)) => {
                    drop(file);
                    if let Err(err) = fs::remove_file(&path)
                        && err.kind() != io::ErrorKind::NotFound
                    {
                        warn!(
                            "failed to remove stale thread writer lock {}: {err}",
                            path.display()
                        );
                    }
                    drop(fallback_lock);
                }
                Ok(TryWriterLockOutcome::WouldBlock) => {}
                Err(err) => {
""",
        1,
    ),
    (
        "        drop(coordination_lock);\n    }\n}\n",
        "        drop(self.fallback_lock.take());\n        drop(coordination_lock);\n    }\n}\n",
        1,
    ),
)

_WRITER_LOCK_TAIL = '#[cfg(test)]\n#[path = "writer_lock_tests.rs"]\n'
_WRITER_LOCK_FALLBACK_ARMS = """        TryFileLockOutcome::Unsupported => match try_acquire_sibling_lock_dir(path)? {
            TryLockDirOutcome::Acquired(guard) => Ok(TryWriterLockOutcome::Acquired(Some(guard))),
            TryLockDirOutcome::WouldBlock => Ok(TryWriterLockOutcome::WouldBlock),
        },
"""

# Edits that carry the downstream delta over the paired-baseline lock code.
WRITER_LOCK_BASELINE_EDITS = (
    (
        "use codex_protocol::ThreadId;\n",
        _UNSUPPORTED_LOCK_BASE_IMPORTS,
        1,
    ),
    (
        """    fn lock_coordination(&self) -> io::Result<File> {
        fs::create_dir_all(&self.directory)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.directory.join(COORDINATION_LOCK_FILE))?;
        file.lock().map_err(|err| {
            io::Error::other(format!(
                "failed to acquire thread writer coordination lock: {err}"
            ))
        })?;
        Ok(file)
    }
""",
        """    fn lock_coordination(&self) -> io::Result<CoordinationLockGuard> {
        fs::create_dir_all(&self.directory)?;
        let path = self.directory.join(COORDINATION_LOCK_FILE);
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)?;
        let fallback_lock = match lock_exclusive_optional(&file).map_err(|err| {
            io::Error::other(format!(
                "failed to acquire thread writer coordination lock: {err}"
            ))
        })? {
            FileLockOutcome::Acquired => None,
            FileLockOutcome::Unsupported => Some(acquire_sibling_lock_dir(&path)?),
        };
        Ok(CoordinationLockGuard {
            _file: file,
            _fallback_lock: fallback_lock,
        })
    }
""",
        1,
    ),
    (
        _WRITER_LOCK_TAIL,
        f"""fn try_lock_writer_file(file: &File, path: &Path) -> io::Result<TryWriterLockOutcome> {{
    match try_lock_exclusive_optional(file)? {{
        TryFileLockOutcome::Acquired => Ok(TryWriterLockOutcome::Acquired(None)),
        TryFileLockOutcome::WouldBlock => Ok(TryWriterLockOutcome::WouldBlock),
{_WRITER_LOCK_FALLBACK_ARMS}    }}
}}

{_WRITER_LOCK_TAIL}""",
        1,
    ),
)

# Edits that apply the same downstream behavior to upstream's bounded-wait,
# shared-coordination redesign.
WRITER_LOCK_UPSTREAM_EDITS = (
    (
        "use codex_protocol::ThreadId;\n",
        _UNSUPPORTED_LOCK_UPSTREAM_IMPORTS,
        1,
    ),
    (
        """        let file = self.open_coordination()?;
        // Never queue a directory sweep behind another writer. Removal remains exclusive.
        let have_exclusive = !self.cleanup_attempted.swap(true, Ordering::Relaxed)
            && match file.try_lock() {
                Ok(()) => true,
                Err(fs::TryLockError::WouldBlock) => false,
                Err(fs::TryLockError::Error(err)) => return Err(err),
            };
        if have_exclusive {
            if let Err(err) = self.remove_stale_thread_locks() {
                warn!("failed to clean up stale thread writer locks: {err}");
            }
        } else {
            Self::wait_for_coordination(|| file.try_lock_shared())?;
        }
        let _coordination_lock = file;
""",
        """        let file = self.open_coordination()?;
        let coordination_path = self.coordination_path();
        let mut coordination_fallback = None;
        // Never queue a directory sweep behind another writer. Removal remains exclusive.
        let have_exclusive = !self.cleanup_attempted.swap(true, Ordering::Relaxed)
            && match try_lock_writer_file(&file, &coordination_path)? {
                TryWriterLockOutcome::Acquired(fallback_lock) => {
                    coordination_fallback = fallback_lock;
                    true
                }
                TryWriterLockOutcome::WouldBlock => false,
            };
        if have_exclusive {
            if let Err(err) = self.remove_stale_thread_locks() {
                warn!("failed to clean up stale thread writer locks: {err}");
            }
        } else {
            coordination_fallback = Self::wait_for_coordination(|| {
                try_lock_coordination_shared(&file, &coordination_path)
            })?;
        }
        let _coordination_lock = CoordinationLockGuard {
            _file: file,
            _fallback_lock: coordination_fallback,
        };
""",
        1,
    ),
    (
        """            .open(self.directory.join(COORDINATION_LOCK_FILE))
    }

    fn lock_coordination(&self) -> io::Result<File> {
        let file = self.open_coordination()?;
        Self::wait_for_coordination(|| file.try_lock())?;
        Ok(file)
    }

    fn wait_for_coordination(
        try_lock: impl Fn() -> Result<(), fs::TryLockError>,
    ) -> io::Result<()> {
""",
        """            .open(self.coordination_path())
    }

    fn coordination_path(&self) -> PathBuf {
        self.directory.join(COORDINATION_LOCK_FILE)
    }

    fn lock_coordination(&self) -> io::Result<CoordinationLockGuard> {
        let file = self.open_coordination()?;
        let path = self.coordination_path();
        let fallback_lock = Self::wait_for_coordination(|| try_lock_writer_file(&file, &path))?;
        Ok(CoordinationLockGuard {
            _file: file,
            _fallback_lock: fallback_lock,
        })
    }

    fn wait_for_coordination(
        try_lock: impl Fn() -> io::Result<TryWriterLockOutcome>,
    ) -> io::Result<Option<LockDirGuard>> {
""",
        1,
    ),
    (
        """            match try_lock() {
                Ok(()) => return Ok(()),
                Err(fs::TryLockError::WouldBlock) if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(/*millis*/ 50));
                }
                Err(fs::TryLockError::WouldBlock) => {
""",
        """            match try_lock()? {
                TryWriterLockOutcome::Acquired(fallback_lock) => return Ok(fallback_lock),
                TryWriterLockOutcome::WouldBlock if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(/*millis*/ 50));
                }
                TryWriterLockOutcome::WouldBlock => {
""",
        1,
    ),
    (
        """                }
                Err(fs::TryLockError::Error(err)) => return Err(err),
            }
        }
    }
""",
        """                }
            }
        }
    }
""",
        1,
    ),
    (
        """        match coordination_lock.try_lock() {
            Ok(()) => {}
            // Leave the file for the next writer. Dropping the guard still closes it.
            Err(fs::TryLockError::WouldBlock) => return,
            Err(fs::TryLockError::Error(err)) => {
                warn!("failed to coordinate thread writer lock cleanup: {err}");
                return;
            }
        }
""",
        """        let coordination_path = self.coordinator.coordination_path();
        let _coordination_fallback =
            match try_lock_writer_file(&coordination_lock, &coordination_path) {
                Ok(TryWriterLockOutcome::Acquired(fallback_lock)) => fallback_lock,
                // Leave the file for the next writer. Dropping the guard still closes it.
                Ok(TryWriterLockOutcome::WouldBlock) => return,
                Err(err) => {
                    warn!("failed to coordinate thread writer lock cleanup: {err}");
                    return;
                }
            };
""",
        1,
    ),
    (
        _WRITER_LOCK_TAIL,
        f"""fn try_lock_writer_file(file: &File, path: &Path) -> io::Result<TryWriterLockOutcome> {{
    lock_or_fallback(try_lock_exclusive_optional(file)?, path)
}}

/// Shared coordination lets writers open together; the fallback directory is exclusive.
fn try_lock_coordination_shared(file: &File, path: &Path) -> io::Result<TryWriterLockOutcome> {{
    lock_or_fallback(try_lock_shared_optional(file)?, path)
}}

fn lock_or_fallback(outcome: TryFileLockOutcome, path: &Path) -> io::Result<TryWriterLockOutcome> {{
    match outcome {{
        TryFileLockOutcome::Acquired => Ok(TryWriterLockOutcome::Acquired(None)),
        TryFileLockOutcome::WouldBlock => Ok(TryWriterLockOutcome::WouldBlock),
{_WRITER_LOCK_FALLBACK_ARMS}    }}
}}

{_WRITER_LOCK_TAIL}""",
        1,
    ),
)


def adapt_writer_lock(baseline, source, upstream):
    """Carry lock fallbacks through upstream's bounded-wait coordination redesign."""
    redesign = "    fn open_coordination(&self) -> io::Result<File> {\n"
    if (
        redesign not in upstream
        or redesign in baseline
        or source == baseline
        or "try_lock_exclusive_optional" not in source
    ):
        return source
    expected = baseline
    for before, after, count in (
        *WRITER_LOCK_COMMON_EDITS,
        *WRITER_LOCK_BASELINE_EDITS,
    ):
        if expected.count(before) != count:
            raise ValueError("unrecognized writer-lock baseline; refusing adaptation")
        expected = expected.replace(before, after)
    if source != expected:
        raise ValueError("unrecognized Termux writer-lock delta; refusing adaptation")
    adapted = upstream
    for before, after, count in (
        *WRITER_LOCK_COMMON_EDITS,
        *WRITER_LOCK_UPSTREAM_EDITS,
    ):
        if adapted.count(before) != count:
            raise ValueError("unrecognized writer-lock code; refusing adaptation")
        adapted = adapted.replace(before, after)
    return adapted


ARG0_IMPORT = "use codex_sandboxing::landlock::CODEX_LINUX_SANDBOX_ARG0;\n"
ARG0_RENAMED_IMPORT = "use codex_sandboxing::linux::CODEX_LINUX_SANDBOX_ARG0;\n"


def adapt_arg0_sandbox_import(baseline, source, upstream):
    """Carry the arg0 lock fallback across upstream's sandbox module rename."""
    if (
        ARG0_IMPORT not in baseline
        or ARG0_IMPORT in upstream
        or ARG0_RENAMED_IMPORT not in upstream
        or source == baseline
        or "try_lock_exclusive_optional" not in source
    ):
        return source
    edits = ARG0_LOCK_EDITS
    upstream_edits = tuple(
        (
            ARG0_RENAMED_IMPORT if before == ARG0_IMPORT else before,
            after.replace(ARG0_IMPORT, ARG0_RENAMED_IMPORT),
            count,
        )
        for before, after, count in edits
    )
    expected = baseline
    adapted = upstream
    for (before, after, count), (up_before, up_after, _) in zip(
        edits, upstream_edits, strict=True
    ):
        if expected.count(before) != count or adapted.count(up_before) != count:
            raise ValueError("unrecognized arg0 lock code; refusing adaptation")
        expected = expected.replace(before, after)
        adapted = adapted.replace(up_before, up_after)
    if source != expected:
        raise ValueError("unrecognized Termux arg0 lock delta; refusing adaptation")
    return adapted


AMEND_PATH = "codex-rs/execpolicy/src/amend.rs"
_AMEND_IMPORTS = (
    "use codex_utils_file_lock::FileLockOutcome;\n"
    "use codex_utils_file_lock::acquire_sibling_lock_dir;\n"
    "use codex_utils_file_lock::lock_exclusive_optional;\n\n"
)
_AMEND_LOCK = (
    "    file.lock().map_err(|source| AmendError::LockPolicyFile {\n"
    "        path: policy_path.to_path_buf(),\n"
    "        source,\n"
    "    })?;\n"
)
_AMEND_GUARDED_LOCK = """    let _lock_dir_guard =
        match lock_exclusive_optional(&file).map_err(|source| AmendError::LockPolicyFile {
            path: policy_path.to_path_buf(),
            source,
        })? {
            FileLockOutcome::Acquired => None,
            FileLockOutcome::Unsupported => {
                Some(acquire_sibling_lock_dir(policy_path).map_err(|source| {
                    AmendError::LockPolicyFile {
                        path: policy_path.to_path_buf(),
                        source,
                    }
                })?)
            }
        };
"""


def adapt_execpolicy_decision_import(baseline, source, upstream):
    """Carry the policy-file lock fallback across upstream's Decision re-export."""
    old = "use crate::decision::Decision;\n"
    new = "use crate::Decision;\n"
    if (
        old not in baseline
        or old in upstream
        or new not in upstream
        or source == baseline
        or "lock_exclusive_optional" not in source
    ):
        return source
    edits = (
        (old, _AMEND_IMPORTS + old, new, _AMEND_IMPORTS + new),
        (_AMEND_LOCK, _AMEND_GUARDED_LOCK, _AMEND_LOCK, _AMEND_GUARDED_LOCK),
    )
    expected = baseline
    adapted = upstream
    for before, after, up_before, up_after in edits:
        if expected.count(before) != 1 or adapted.count(up_before) != 1:
            raise ValueError("unrecognized execpolicy lock code; refusing adaptation")
        expected = expected.replace(before, after)
        adapted = adapted.replace(up_before, up_after)
    if source != expected:
        raise ValueError(
            "unrecognized Termux execpolicy lock delta; refusing adaptation"
        )
    return adapted


CLIPBOARD_PATH = "codex-rs/tui/src/clipboard_paste.rs"
_PASTE_ERROR = "#[derive(Debug, Clone)]\npub enum PasteImageError {"
_ANDROID_PASTE_PNG = """/// Android/Termux does not support arboard; return a clear error.
#[cfg(target_os = "android")]
pub fn paste_image_as_png() -> Result<(Vec<u8>, PastedImageInfo), PasteImageError> {
    Err(PasteImageError::ClipboardUnavailable(
        "clipboard image paste is unsupported on Android".into(),
    ))
}

"""
_ANDROID_PASTE_COMMENT = "    // Keep error consistent with paste_image_as_png.\n"
_BUILDER_IMPORT = "use tempfile::Builder;\n"
CLIPBOARD_EDITS = (
    (
        _PASTE_ERROR,
        "/// Shared clipboard dispatch uses these errors on every platform.\n"
        '#[derive(Debug, Clone)]\n#[cfg_attr(target_os = "android", allow(dead_code))]\n'
        "pub enum PasteImageError {",
    ),
    (_ANDROID_PASTE_PNG, ""),
    (_ANDROID_PASTE_COMMENT, ""),
)


def adapt_android_clipboard_cleanup(baseline, source, upstream):
    """Retire Android-only clipboard dead code while keeping upstream's image helpers."""
    persist = "pub(crate) fn persist_pasted_image("
    if persist not in upstream or persist in baseline or source == baseline:
        return source
    # Upstream now uses tempfile's Builder on every platform, so only the
    # baseline-only cfg on that import is dropped from the port.
    expected = baseline.replace(
        _BUILDER_IMPORT,
        '#[cfg(not(target_os = "android"))]\n' + _BUILDER_IMPORT,
    )
    adapted = upstream
    for before, after in CLIPBOARD_EDITS:
        if expected.count(before) != 1 or adapted.count(before) != 1:
            raise ValueError("unrecognized clipboard code; refusing adaptation")
        expected = expected.replace(before, after)
        adapted = adapted.replace(before, after)
    if expected.count(_BUILDER_IMPORT) != 1 or source != expected:
        raise ValueError("unrecognized Termux clipboard delta; refusing adaptation")
    return adapted


CORE_INSTALLATION_ID_PATH = "codex-rs/core/src/installation_id.rs"
INSTALLATION_ID_PATH = "codex-rs/runtime-agent-instance/src/installation_id.rs"
INSTALLATION_ID_MANIFEST = "codex-rs/runtime-agent-instance/Cargo.toml"
_ID_IMPORT = "\nuse codex_utils_absolute_path::AbsolutePathBuf;\n"
_ID_IMPORTS = (
    "use codex_utils_file_lock::FileLockOutcome;\n"
    "use codex_utils_file_lock::acquire_sibling_lock_dir;\n"
    "use codex_utils_file_lock::lock_exclusive_optional;\n"
)
_ID_UPSTREAM_IMPORTS = "use codex_utils_file_lock::acquire_sibling_lock_dir;\n"
_ID_BASELINE_LOCK = "        file.lock()?;\n"
_ID_BASELINE_GUARDED_LOCK = """        let _lock_dir_guard = match lock_exclusive_optional(&file)? {
            FileLockOutcome::Acquired => None,
            FileLockOutcome::Unsupported => Some(acquire_sibling_lock_dir(&path)?),
        };
"""
_ID_UPSTREAM_LOCK = """        file.lock_shared()?;
        let mut contents = String::new();
        file.read_to_string(&mut contents)?;
        if Uuid::parse_str(contents.trim()).is_err() {
            // Another process can initialize the ID while we switch locks. Read it again
            // under the exclusive lock before deciding whether to create a new one.
            file.unlock()?;
            file.lock()?;
"""
_ID_UPSTREAM_GUARDED_LOCK = """        let mut lock_dir_guard = None;
        match file.lock_shared() {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::Unsupported => {
                lock_dir_guard = Some(acquire_sibling_lock_dir(&path)?);
            }
            Err(err) => return Err(err),
        }
        let mut contents = String::new();
        file.read_to_string(&mut contents)?;
        if Uuid::parse_str(contents.trim()).is_err() {
            // Another process can initialize the ID while we switch locks. Read it again
            // under the exclusive lock before deciding whether to create a new one.
            if lock_dir_guard.is_none() {
                file.unlock()?;
                file.lock()?;
            }
"""
_ID_MANIFEST_ANCHOR = "codex-utils-absolute-path = { workspace = true }\n"
_ID_MANIFEST_DEP = "codex-utils-file-lock = { workspace = true }\n"


def adapt_installation_id(baseline, source, upstream):
    """Carry the unsupported-lock fallback into upstream's relocated installation ID code."""
    if (
        _ID_BASELINE_LOCK not in baseline
        or "file.lock_shared()?;" not in upstream
        or source == baseline
        or "lock_exclusive_optional" not in source
    ):
        return None
    expected = baseline
    for before, after in (
        (_ID_IMPORT, _ID_IMPORT + _ID_IMPORTS),
        (_ID_BASELINE_LOCK, _ID_BASELINE_GUARDED_LOCK),
    ):
        if expected.count(before) != 1:
            raise ValueError(
                "unrecognized installation-ID baseline; refusing adaptation"
            )
        expected = expected.replace(before, after)
    if source != expected:
        raise ValueError(
            "unrecognized Termux installation-ID delta; refusing adaptation"
        )
    adapted = upstream
    for before, after in (
        (_ID_IMPORT, _ID_IMPORT + _ID_UPSTREAM_IMPORTS),
        (_ID_UPSTREAM_LOCK, _ID_UPSTREAM_GUARDED_LOCK),
    ):
        if adapted.count(before) != 1:
            raise ValueError("unrecognized installation-ID code; refusing adaptation")
        adapted = adapted.replace(before, after)
    return adapted


def add_file_lock_dependency(manifest):
    if manifest.count(_ID_MANIFEST_ANCHOR) != 1 or _ID_MANIFEST_DEP in manifest:
        raise ValueError("unrecognized installation-ID manifest; refusing adaptation")
    return manifest.replace(
        _ID_MANIFEST_ANCHOR, _ID_MANIFEST_ANCHOR + _ID_MANIFEST_DEP, 1
    )


def adapt_android_package_validation(baseline, source, upstream):
    """Carry Android package checks through upstream's entrypoint-aware validation."""
    if (
        "fn validate_package(root: &Path, entrypoint: &str)" not in upstream
        or "fn validate_package(root: &Path, entrypoint: &str)" in baseline
        or source == baseline
        or 'target_os = "android"' not in source
    ):
        return source
    return port_exact_edits(
        baseline, source, upstream, ANDROID_PACKAGE_EDITS, "Android package validation"
    )


def release_tree(upstream, source, baseline, excluded):
    upstream, source, baseline = (
        git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
        for ref in (upstream, source, baseline)
    )
    # A private index lets failed preflights leave the caller's checkout intact.
    with tempfile.TemporaryDirectory(
        prefix="termux-release-tree-",
        dir=os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR"),
    ) as scratch:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(scratch) / "index"))
        cli = "codex-rs/cli/src/main.rs"
        source_cli = git("show", f"{source}:{cli}")
        has_termux_updater = b"async fn run_termux_update()" in source_cli
        socket_path = "codex-rs/app-server-transport/src/transport/unix_socket.rs"
        adapted_socket = None
        if all(
            git("ls-tree", ref, "--", socket_path)
            for ref in (baseline, source, upstream)
        ):
            source_socket = git("show", f"{source}:{socket_path}").decode()
            upstream_socket = git("show", f"{upstream}:{socket_path}").decode()
            baseline_socket = git("show", f"{baseline}:{socket_path}").decode()
            adapted = adapt_removable_startup_lock(
                baseline_socket, source_socket, upstream_socket
            )
            if adapted == source_socket:
                adapted = adapt_split_daemon_socket(
                    baseline_socket, source_socket, upstream_socket
                )
            if adapted != source_socket:
                adapted_socket = adapted
                print(
                    "Adapted Termux fallback to removable startup locks.",
                    file=sys.stderr,
                )

        adapted_files = {}
        for path, adapter in (
            (ARG0_PATH, adapt_arg0_lock),
            (WRITER_LOCK_PATH, adapt_writer_lock),
            (AMEND_PATH, adapt_execpolicy_decision_import),
            (CLIPBOARD_PATH, adapt_android_clipboard_cleanup),
            (DAEMON_PREPARE_PATH, adapt_android_package_validation),
        ):
            if not all(
                git("ls-tree", ref, "--", path) for ref in (baseline, source, upstream)
            ):
                continue
            texts = [
                git("show", f"{ref}:{path}").decode()
                for ref in (baseline, source, upstream)
            ]
            adapted = adapter(*texts)
            if adapted != texts[1]:
                adapted_files[path] = (adapted, texts[2])
                print(
                    f"Adapted Termux delta in {path} to the new upstream API.",
                    file=sys.stderr,
                )

        moved_id = None
        if (
            git("ls-tree", baseline, "--", CORE_INSTALLATION_ID_PATH)
            and git("ls-tree", source, "--", CORE_INSTALLATION_ID_PATH)
            and not git("ls-tree", upstream, "--", CORE_INSTALLATION_ID_PATH)
            and git("ls-tree", upstream, "--", INSTALLATION_ID_PATH)
        ):
            texts = [
                git("show", f"{ref}:{CORE_INSTALLATION_ID_PATH}").decode()
                for ref in (baseline, source)
            ]
            upstream_id = git("show", f"{upstream}:{INSTALLATION_ID_PATH}").decode()
            adapted = adapt_installation_id(*texts, upstream_id)
            if adapted is not None:
                manifest = git(
                    "show", f"{upstream}:{INSTALLATION_ID_MANIFEST}"
                ).decode()
                core_manifest = git(
                    "show", f"{source}:codex-rs/core/Cargo.toml"
                ).decode()
                # Core no longer locks files once the installation ID code moves out.
                if core_manifest.count(_ID_MANIFEST_DEP) != 1:
                    raise ValueError("unrecognized core file-lock dependency")
                moved_id = (
                    adapted,
                    upstream_id,
                    add_file_lock_dependency(manifest),
                    manifest,
                    core_manifest.replace(_ID_MANIFEST_DEP, ""),
                )
                print(
                    f"Adapted Termux delta in {INSTALLATION_ID_PATH} to the new upstream API.",
                    file=sys.stderr,
                )

        def put_blob(path, content):
            blob = git("hash-object", "-w", "--stdin", data=content).decode().strip()
            git(
                "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env
            )

        def code_tree(ref):
            git("read-tree", ref, env=env)
            for path in excluded:
                present = (
                    subprocess.run(
                        ["git", "cat-file", "-e", f"{upstream}:{path}"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    ).returncode
                    == 0
                )
                if present or git("ls-files", "--", path, env=env):
                    git(
                        "restore",
                        f"--source={upstream}",
                        "--staged",
                        "--",
                        path,
                        env=env,
                    )
            if ref == source:
                # Package version is release metadata, not a compatibility edit.
                path = "codex-rs/Cargo.toml"
                original = git("show", f"{baseline}:{path}")
                current = git("show", f"{source}:{path}")
                version = re.compile(
                    rb'(\[workspace\.package\]\n(?:(?!\[).)*?^version\s*=\s*)"[^"]+"',
                    re.MULTILINE | re.DOTALL,
                )
                match = version.search(original)
                if not match or not version.search(current):
                    raise ValueError(
                        "workspace.package.version missing from patch baseline/source"
                    )
                normalized = version.sub(
                    lambda m: m[1] + match[0][len(match[1]) :], current, count=1
                )
                put_blob(path, normalized)
                path = "codex-rs/tui/src/clipboard_copy.rs"
                if all(
                    git("ls-tree", ref, "--", path)
                    for ref in (baseline, source, upstream)
                ):
                    original = git("show", f"{baseline}:{path}")
                    current = git("show", f"{source}:{path}")
                    normalized = drop_retired_clipboard_guard(
                        original, current, git("show", f"{upstream}:{path}")
                    )
                    if normalized != current:
                        put_blob(path, normalized)
                        print(
                            "Retired Android warning guards for removed SuppressStderr helper.",
                            file=sys.stderr,
                        )
            if adapted_socket is not None:
                # The adapter verifies the entire old downstream delta before
                # porting it. Merge that reviewed delta against the new API.
                put_blob(
                    socket_path,
                    (adapted_socket if ref == source else upstream_socket).encode(),
                )
            for path, (adapted, upstream_text) in adapted_files.items():
                put_blob(path, (adapted if ref == source else upstream_text).encode())
            if moved_id is not None:
                git(
                    "update-index",
                    "--force-remove",
                    CORE_INSTALLATION_ID_PATH,
                    env=env,
                )
                adapted_id, upstream_id, adapted_manifest, upstream_manifest, core = (
                    moved_id
                )
                put_blob(
                    INSTALLATION_ID_PATH,
                    (adapted_id if ref == source else upstream_id).encode(),
                )
                put_blob(
                    INSTALLATION_ID_MANIFEST,
                    (adapted_manifest if ref == source else upstream_manifest).encode(),
                )
                if ref == source:
                    put_blob("codex-rs/core/Cargo.toml", core.encode())
            if has_termux_updater:
                put_blob(
                    cli, async_updater(git("show", f"{ref}:{cli}").decode()).encode()
                )
            return git("write-tree", env=env).decode().strip()

        base_tree = code_tree(baseline)
        source_tree = code_tree(source)
        upstream_tree = code_tree(upstream)
        # Git 2.43 (used by ubuntu-slim) requires commits for merge-tree's
        # --merge-base and branch arguments. Keep normalized trees in private,
        # deterministic commit objects without updating any refs.
        commit_env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Termux release tree",
            GIT_AUTHOR_EMAIL="termux-release-tree@users.noreply.github.com",
            GIT_AUTHOR_DATE="2000-01-01T00:00:00 +0000",
            GIT_COMMITTER_NAME="Termux release tree",
            GIT_COMMITTER_EMAIL="termux-release-tree@users.noreply.github.com",
            GIT_COMMITTER_DATE="2000-01-01T00:00:00 +0000",
        )

        def commit_tree(tree, parent=None):
            args = ["-c", "commit.gpgsign=false", "commit-tree", tree]
            if parent:
                args.extend(("-p", parent))
            args.extend(("-m", "Normalized Termux release merge input"))
            return git(*args, env=commit_env).decode().strip()

        base_commit = commit_tree(base_tree)
        upstream_commit = commit_tree(upstream_tree, base_commit)
        source_commit = commit_tree(source_tree, base_commit)
        merged = subprocess.run(
            [
                "git",
                "-c",
                "merge.conflictStyle=diff3",
                "merge-tree",
                "--write-tree",
                "--name-only",
                "--no-messages",
                f"--merge-base={base_commit}",
                upstream_commit,
                source_commit,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        result = merged.stdout.splitlines()[0] if merged.stdout else ""
        conflicts = merged.stdout.splitlines()[1:]
        if merged.returncode not in (0, 1) or not re.fullmatch(
            r"[0-9a-f]{40,64}", result
        ):
            raise ValueError(
                "release merge could not produce a tree; no release refs changed:\n"
                + merged.stdout
                + merged.stderr
            )
        if merged.returncode == 1:
            git("read-tree", result, env=env)
            remaining = []
            for path in conflicts:
                updater_path = path in (
                    cli,
                    "codex-rs/tui/src/update_action.rs",
                    "codex-rs/tui/src/update_prompt.rs",
                    "codex-rs/tui/src/updates.rs",
                )
                dependency_path = path == "codex-rs/Cargo.lock" or (
                    path.startswith("codex-rs/") and path.endswith("/Cargo.toml")
                )
                if not (has_termux_updater and updater_path) and not dependency_path:
                    remaining.append(path)
                    continue
                text = git("show", f"{result}:{path}").decode()
                if has_termux_updater and updater_path:
                    text = merge_updater_additions(text, path=path)
                if dependency_path:
                    text = merge_dependency_additions(text, path=path)
                if re.search(r"^(?:<<<<<<<|=======|>>>>>>>)", text, re.MULTILINE):
                    remaining.append(path)
                else:
                    put_blob(path, text.encode())
            if not remaining:
                result = git("write-tree", env=env).decode().strip()
                merged.returncode = 0
        if merged.returncode:
            raise ValueError(
                "Termux delta conflicts with requested upstream; no release refs changed:\n"
                + merged.stdout
                + merged.stderr
            )
        if git("cat-file", "-t", result).strip() != b"tree":
            raise ValueError("release merge did not produce a tree")

        allowed = paths_between(base_tree, source_tree)
        # Git can carry a modified downstream file through an upstream rename.
        changes = git(
            "diff", "--name-status", "-z", "--find-renames", base_tree, upstream
        ).split(b"\0")
        i = 0
        while i < len(changes) and changes[i]:
            status, old = changes[i : i + 2]
            i += 2
            if status.startswith(b"R"):
                new = changes[i]
                i += 1
                if old in allowed:
                    allowed.add(new)
        unexpected = paths_between(upstream, result) - allowed
        if unexpected:
            raise ValueError(
                "release contains changes outside the downstream delta:\n"
                + "\n".join(sorted(p.decode() for p in unexpected))
            )
        validate_manifests(result)
        print(
            f"Verified release tree: {len(paths_between(upstream, result))} downstream paths; "
            "all other paths match requested upstream.",
            file=sys.stderr,
        )
        return result


if __name__ == "__main__":
    try:
        if len(sys.argv) < 4:
            raise ValueError(
                "usage: termux-release-tree.py UPSTREAM SOURCE PAIRED_UPSTREAM [EXCLUDED_PATH ...]"
            )
        print(release_tree(*sys.argv[1:4], [".github", *sys.argv[4:]]))
    except (ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
