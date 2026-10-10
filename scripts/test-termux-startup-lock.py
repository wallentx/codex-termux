#!/usr/bin/env python3
"""Test the real startup-lock port with std-only Rust, without building Codex."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "release_tree", ROOT / "scripts/termux-release-tree.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
BASELINE = "rust-v0.162.0-alpha.16"
SOURCE = BASELINE + "-termux"
UPSTREAM = "rust-v0.162.1"
SOCKET = "codex-rs/app-server-transport/src/transport/unix_socket.rs"


def read(ref, path):
    return subprocess.check_output(
        ["git", "show", f"{ref}:{path}"], cwd=ROOT, text=True
    )


def between(text, start, end):
    return text.split(start, 1)[1].split(end, 1)[0]


RUST_TESTS = r"""
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TestDir(std::path::PathBuf);
    impl TestDir {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let id = NEXT.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!("startup-lock-{}-{id}", std::process::id()));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn lock(&self) -> AbsolutePathBuf { self.0.join("startup.lock") }
    }
    impl Drop for TestDir {
        fn drop(&mut self) { std::fs::remove_dir_all(&self.0).unwrap(); }
    }

    #[test]
    fn blocking_constructor_keeps_the_fallback_until_drop() {
        let dir = TestDir::new();
        let lock = acquire_blocking(dir.lock()).unwrap();
        #[cfg(force_unsupported)]
        assert!(lock._lock_dir_guard.is_some());
        assert!(lock.removable_path.is_none());
        assert!(!lock.remove_on_drop);
        drop(lock);
        assert!(!codex_utils_file_lock::sibling_lock_dir(&dir.lock()).exists());
        drop(try_acquire_removable_app_server_startup_lock(dir.lock()).unwrap());
    }

    #[test]
    fn acquisition_is_exclusive_and_releases_on_drop() {
        let dir = TestDir::new();
        let first = try_acquire_removable_app_server_startup_lock(dir.lock()).unwrap();
        #[cfg(force_unsupported)]
        assert!(first._lock_dir_guard.is_some());
        let error = try_acquire_removable_app_server_startup_lock(dir.lock()).err().unwrap();
        assert_eq!(error.kind(), ErrorKind::WouldBlock);
        drop(first);
        drop(try_acquire_removable_app_server_startup_lock(dir.lock()).unwrap());
    }

    #[test]
    fn publication_releases_lock_and_cleanup_respects_a_contender() {
        let dir = TestDir::new();
        let mut first = try_acquire_removable_app_server_startup_lock(dir.lock()).unwrap();
        first.remove_on_drop = true;
        let first = publish(first).unwrap();
        assert!(first._lock_dir_guard.is_none());
        let second = try_acquire_removable_app_server_startup_lock(dir.lock()).unwrap();
        drop(first);
        assert!(dir.lock().exists());
        drop(second);
        assert!(!codex_utils_file_lock::sibling_lock_dir(&dir.lock()).exists());
    }

    #[test]
    fn cleanup_reacquires_and_removes_the_file_after_publication() {
        let dir = TestDir::new();
        let mut lock = try_acquire_removable_app_server_startup_lock(dir.lock()).unwrap();
        lock.remove_on_drop = true;
        drop(publish(lock).unwrap());
        assert!(!dir.lock().exists());
        assert!(!codex_utils_file_lock::sibling_lock_dir(&dir.lock()).exists());
    }

    #[test]
    fn cleanup_before_publication_does_not_reacquire_its_own_directory() {
        let dir = TestDir::new();
        let mut lock = try_acquire_removable_app_server_startup_lock(dir.lock()).unwrap();
        lock.remove_on_drop = true;
        drop(lock);
        assert!(!dir.lock().exists());
        assert!(!codex_utils_file_lock::sibling_lock_dir(&dir.lock()).exists());
    }

    #[test]
    fn cleanup_never_removes_a_replacement_inode() {
        let dir = TestDir::new();
        let mut lock = try_acquire_removable_app_server_startup_lock(dir.lock()).unwrap();
        lock.remove_on_drop = true;
        let lock = publish(lock).unwrap();
        std::fs::rename(dir.lock(), dir.0.join("old.lock")).unwrap();
        std::fs::write(dir.lock(), "replacement").unwrap();
        drop(lock);
        assert_eq!(std::fs::read_to_string(dir.lock()).unwrap(), "replacement");
        assert!(!codex_utils_file_lock::sibling_lock_dir(&dir.lock()).exists());
    }
"""


class StartupLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = read(BASELINE, SOCKET)
        cls.source = read(SOURCE, SOCKET)
        cls.upstream = read(UPSTREAM, SOCKET)
        cls.adapted = module.adapt_removable_startup_lock(
            cls.baseline, cls.source, cls.upstream
        )

    def test_unknown_downstream_edits_are_rejected(self):
        with self.assertRaisesRegex(
            ValueError, "unrecognized Termux startup-lock delta"
        ):
            module.adapt_removable_startup_lock(
                self.baseline,
                self.source + "\n// another downstream edit\n",
                self.upstream,
            )

    def test_changed_upstream_lock_operations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unrecognized removable startup-lock"):
            module.adapt_removable_startup_lock(
                self.baseline,
                self.source,
                self.upstream.replace("    file.try_lock()?;", "    another_lock()?;"),
            )

    def test_old_upstream_and_checkpointed_ports_are_not_rewritten(self):
        self.assertEqual(
            module.adapt_removable_startup_lock(
                self.baseline, self.source, self.baseline
            ),
            self.source,
        )
        self.assertEqual(
            module.adapt_removable_startup_lock(
                self.upstream, self.adapted, self.upstream
            ),
            self.adapted,
        )

    def test_generated_rust_lock_lifecycle(self):
        helper = read(SOURCE, "codex-rs/utils/file-lock/src/lib.rs")
        # Inject only the unsupported backend result. Keep the actual atomic
        # directory implementation and all generated startup-lock code intact.
        for name, outcome in (
            ("lock_exclusive_optional", "FileLockOutcome"),
            ("try_lock_exclusive_optional", "TryFileLockOutcome"),
        ):
            helper = helper.replace(f"pub fn {name}(", f"pub fn platform_{name}(")
            helper += f"""
pub fn {name}(file: &File) -> io::Result<{outcome}> {{
    #[cfg(force_unsupported)]
    {{ let _ = file; Ok({outcome}::Unsupported) }}
    #[cfg(not(force_unsupported))]
    {{ platform_{name}(file) }}
}}
"""
        struct = "pub struct AppServerStartupLock {" + between(
            self.adapted,
            "pub struct AppServerStartupLock {",
            "pub async fn acquire_app_server_startup_lock(",
        )
        sync_code = "impl AppServerStartupLock {" + between(
            self.adapted,
            "impl AppServerStartupLock {",
            "#[cfg(unix)]\nasync fn set_control_socket_permissions(",
        )
        blocking = between(
            self.adapted,
            "tokio::task::spawn_blocking(move || {\n",
            "\n    })\n",
        )
        unlock = "    let socket_guard = {\n" + between(
            self.adapted, "    let socket_guard = {\n", "    info!("
        )
        harness = (
            "#![allow(dead_code, unused_imports)]\n"
            "macro_rules! warn { ($($args:tt)*) => {}; }\n"
            + "mod codex_utils_file_lock {\n"
            + helper
            + "\n}\nmod startup {\n"
            + "use super::codex_utils_file_lock::{self, *};\n"
            + "use std::fs::OpenOptions;\nuse std::path::Path;\n"
            + "use std::io::{ErrorKind, Result as IoResult};\n"
            + "type AbsolutePathBuf = std::path::PathBuf;\n"
            + struct
            + sync_code
            + "fn acquire_blocking(startup_lock_path: AbsolutePathBuf) -> IoResult<AppServerStartupLock> {\n"
            + blocking
            + "\n}\nstruct TestSocketGuard { _startup_lock: AppServerStartupLock }\n"
            + "fn publish(lock: AppServerStartupLock) -> IoResult<AppServerStartupLock> {\n"
            + "let socket_guard = TestSocketGuard { _startup_lock: lock };\n"
            + unlock
            + "Ok(socket_guard._startup_lock)\n}\n"
            + RUST_TESTS
            + "}\n"
        )
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            rust = root / "startup.rs"
            rust.write_text(harness)
            for mode in ("native", "force_unsupported"):
                with self.subTest(mode=mode):
                    binary = root / mode
                    command = [
                        "rustc",
                        "--edition=2024",
                        "--test",
                        str(rust),
                        "-o",
                        str(binary),
                    ]
                    if mode == "force_unsupported":
                        command += ["--cfg", mode]
                    subprocess.run(command, check=True)
                    subprocess.run([str(binary)], check=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
