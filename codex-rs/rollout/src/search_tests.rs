//! Regression coverage for rollout search error handling.

use super::*;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

#[tokio::test]
async fn compressed_search_preserves_error_handling() -> io::Result<()> {
    let root = TempDir::new()?;
    let path = root.path().join("rollout-0001.jsonl.zst");
    std::fs::write(&path, zstd::stream::encode_all(&b""[..], 3)?)?;
    let query = "a".repeat(500_000);

    assert!(case_insensitive_literal_regex(&query).is_err());
    assert_eq!(
        scan_compressed_rollout_matches(root.path(), &query).await?,
        RolloutSearchMatches::new()
    );
    assert_eq!(
        first_rollout_content_match_snippet(&root.path().join("missing.jsonl"), &query)
            .await
            .unwrap_err()
            .kind(),
        io::ErrorKind::NotFound
    );
    Ok(())
}
