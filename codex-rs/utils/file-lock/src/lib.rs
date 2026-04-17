use std::fs::File;
use std::io;

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
