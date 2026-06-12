use std::fs::File;
use std::fs::create_dir;
use std::fs::remove_dir;
use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::thread;
use std::time::Duration;

const LOCK_DIR_RETRY_SLEEP: Duration = Duration::from_millis(100);

/// Result of acquiring a blocking advisory file lock.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FileLockOutcome {
    Acquired,
    Unsupported,
}

/// Result of attempting to acquire a non-blocking advisory file lock.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TryFileLockOutcome {
    Acquired,
    WouldBlock,
    Unsupported,
}

/// Result of attempting to acquire a non-blocking lock directory.
#[derive(Debug, PartialEq, Eq)]
pub enum TryLockDirOutcome {
    Acquired(LockDirGuard),
    WouldBlock,
}

/// Guard for a sibling lock directory created with an atomic `mkdir`.
#[derive(Debug, PartialEq, Eq)]
pub struct LockDirGuard {
    path: PathBuf,
}

impl Drop for LockDirGuard {
    fn drop(&mut self) {
        let _ = remove_dir(&self.path);
    }
}

/// Acquires an exclusive advisory file lock, treating unsupported file locking
/// as a distinct outcome for platforms such as Termux.
pub fn lock_exclusive_optional(file: &File) -> io::Result<FileLockOutcome> {
    match file.lock() {
        Ok(()) => Ok(FileLockOutcome::Acquired),
        Err(err) if err.kind() == io::ErrorKind::Unsupported => Ok(FileLockOutcome::Unsupported),
        Err(err) => Err(err),
    }
}

/// Attempts to acquire an exclusive advisory file lock without blocking,
/// preserving `WouldBlock` and unsupported file locking as distinct outcomes.
pub fn try_lock_exclusive_optional(file: &File) -> io::Result<TryFileLockOutcome> {
    match file.try_lock() {
        Ok(()) => Ok(TryFileLockOutcome::Acquired),
        Err(std::fs::TryLockError::WouldBlock) => Ok(TryFileLockOutcome::WouldBlock),
        Err(std::fs::TryLockError::Error(err)) if err.kind() == io::ErrorKind::Unsupported => {
            Ok(TryFileLockOutcome::Unsupported)
        }
        Err(std::fs::TryLockError::Error(err)) => Err(err),
    }
}

/// Returns the sibling directory path used as a fallback lock for `path`.
pub fn sibling_lock_dir(path: &Path) -> PathBuf {
    let Some(file_name) = path.file_name() else {
        return path.with_file_name(".lock");
    };

    let mut lock_name = file_name.to_os_string();
    lock_name.push(".lock");
    path.with_file_name(lock_name)
}

/// Acquires a sibling lock directory, blocking until it is available.
pub fn acquire_sibling_lock_dir(path: &Path) -> io::Result<LockDirGuard> {
    loop {
        match try_acquire_sibling_lock_dir(path)? {
            TryLockDirOutcome::Acquired(guard) => return Ok(guard),
            TryLockDirOutcome::WouldBlock => thread::sleep(LOCK_DIR_RETRY_SLEEP),
        }
    }
}

/// Attempts to acquire a sibling lock directory without blocking.
pub fn try_acquire_sibling_lock_dir(path: &Path) -> io::Result<TryLockDirOutcome> {
    let lock_dir = sibling_lock_dir(path);
    match create_dir(&lock_dir) {
        Ok(()) => Ok(TryLockDirOutcome::Acquired(LockDirGuard { path: lock_dir })),
        Err(err) if err.kind() == io::ErrorKind::AlreadyExists => Ok(TryLockDirOutcome::WouldBlock),
        Err(err) => Err(err),
    }
}

/// Attempts to acquire a shared advisory file lock without blocking,
/// preserving `WouldBlock` and unsupported file locking as distinct outcomes.
pub fn try_lock_shared_optional(file: &File) -> io::Result<TryFileLockOutcome> {
    match file.try_lock_shared() {
        Ok(()) => Ok(TryFileLockOutcome::Acquired),
        Err(std::fs::TryLockError::WouldBlock) => Ok(TryFileLockOutcome::WouldBlock),
        Err(std::fs::TryLockError::Error(err)) if err.kind() == io::ErrorKind::Unsupported => {
            Ok(TryFileLockOutcome::Unsupported)
        }
        Err(std::fs::TryLockError::Error(err)) => Err(err),
    }
}

#[cfg(test)]
mod tests {
    use super::TryLockDirOutcome;
    use super::sibling_lock_dir;
    use super::try_acquire_sibling_lock_dir;
    use std::fs;
    use std::path::PathBuf;
    use std::time::SystemTime;

    fn unique_temp_file_path(name: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .expect("system clock should be after Unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "codex-file-lock-{name}-{}-{nanos}",
            std::process::id()
        ))
    }

    #[test]
    fn sibling_lock_dir_appends_lock_suffix() {
        let path = PathBuf::from("/tmp/history.jsonl");

        assert_eq!(
            sibling_lock_dir(&path),
            PathBuf::from("/tmp/history.jsonl.lock")
        );
    }

    #[test]
    fn try_acquire_sibling_lock_dir_is_exclusive_until_drop() {
        let path = unique_temp_file_path("exclusive");
        let lock_dir = sibling_lock_dir(&path);
        let _ = fs::remove_dir_all(&lock_dir);

        let guard = match try_acquire_sibling_lock_dir(&path).expect("acquire lock dir") {
            TryLockDirOutcome::Acquired(guard) => guard,
            TryLockDirOutcome::WouldBlock => panic!("first lock attempt should acquire"),
        };
        assert!(lock_dir.is_dir());

        assert!(matches!(
            try_acquire_sibling_lock_dir(&path).expect("try acquire held lock dir"),
            TryLockDirOutcome::WouldBlock
        ));

        drop(guard);
        assert!(!lock_dir.exists());

        let reacquired = try_acquire_sibling_lock_dir(&path).expect("reacquire lock dir");
        assert!(matches!(reacquired, TryLockDirOutcome::Acquired(_)));

        let _ = fs::remove_dir_all(lock_dir);
    }
}
