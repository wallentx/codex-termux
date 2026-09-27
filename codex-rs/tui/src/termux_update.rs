use anyhow::Context;
use anyhow::Result;
use anyhow::bail;
use codex_login::default_client::create_client;
use serde::Deserialize;
use std::cmp::Ordering;
use std::fmt;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use tempfile::Builder as TempBuilder;
use tokio::process::Command;

pub const TERMUX_RELEASES_URL: &str = "https://github.com/wallentx/codex-termux/releases";
const TERMUX_RELEASES_API_URL: &str =
    "https://api.github.com/repos/wallentx/codex-termux/releases?per_page=100";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpdateOutcome {
    pub previous_version: String,
    pub latest_version: String,
    pub tag_name: String,
    pub executable: PathBuf,
    pub updated: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ReleaseVersion {
    major: u64,
    minor: u64,
    patch: u64,
    prerelease: Prerelease,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum Prerelease {
    Pre(PrereleaseId),
    Stable,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
struct PrereleaseId {
    kind: PrereleaseKind,
    number: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum PrereleaseKind {
    Alpha,
    Beta,
    Rc,
}

#[derive(Debug, Clone, Deserialize)]
struct GitHubRelease {
    tag_name: String,
    assets: Vec<GitHubAsset>,
}

#[derive(Debug, Clone, Deserialize)]
struct GitHubAsset {
    name: String,
    browser_download_url: String,
}

#[derive(Debug, Clone)]
struct ReleaseCandidate {
    version: ReleaseVersion,
    tag_name: String,
    asset: GitHubAsset,
}

impl Ord for ReleaseVersion {
    fn cmp(&self, other: &Self) -> Ordering {
        (self.major, self.minor, self.patch, &self.prerelease).cmp(&(
            other.major,
            other.minor,
            other.patch,
            &other.prerelease,
        ))
    }
}

impl PartialOrd for ReleaseVersion {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl fmt::Display for ReleaseVersion {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}.{}", self.major, self.minor, self.patch)?;
        if let Prerelease::Pre(pre) = &self.prerelease {
            let kind = match pre.kind {
                PrereleaseKind::Alpha => "alpha",
                PrereleaseKind::Beta => "beta",
                PrereleaseKind::Rc => "rc",
            };
            write!(f, "-{}.{}", kind, pre.number)?;
        }
        Ok(())
    }
}

pub(crate) fn parse_release_version(version: &str) -> Option<ReleaseVersion> {
    let (base, prerelease) = match version.trim().split_once('-') {
        Some((base, suffix)) => {
            let (kind, number) = suffix.split_once('.')?;
            let kind = match kind {
                "alpha" => PrereleaseKind::Alpha,
                "beta" => PrereleaseKind::Beta,
                "rc" => PrereleaseKind::Rc,
                _ => return None,
            };
            let number = number.parse::<u64>().ok()?;
            (base, Prerelease::Pre(PrereleaseId { kind, number }))
        }
        None => (version.trim(), Prerelease::Stable),
    };

    let mut parts = base.split('.');
    let major = parts.next()?.parse::<u64>().ok()?;
    let minor = parts.next()?.parse::<u64>().ok()?;
    let patch = parts.next()?.parse::<u64>().ok()?;
    if parts.next().is_some() {
        return None;
    }

    Some(ReleaseVersion {
        major,
        minor,
        patch,
        prerelease,
    })
}

pub(crate) fn parse_termux_tag(tag_name: &str) -> Option<ReleaseVersion> {
    let version = tag_name
        .trim()
        .strip_prefix("rust-v")?
        .strip_suffix("-termux")?;
    parse_release_version(version)
}

pub async fn latest_release_version() -> Result<String> {
    let releases = fetch_releases().await?;
    select_latest_candidate(&releases, &termux_asset_name())
        .map(|candidate| candidate.version.to_string())
        .context("no Termux Codex releases with a compatible Android artifact were found")
}

pub async fn update_current_exe(current_version: &str) -> Result<UpdateOutcome> {
    ensure_termux_build()?;

    let current = parse_release_version(current_version)
        .with_context(|| format!("failed to parse current Codex version '{current_version}'"))?;
    let releases = fetch_releases().await?;
    let Some(candidate) = select_newer_candidate(&releases, &current, &termux_asset_name()) else {
        return Ok(UpdateOutcome {
            previous_version: current_version.to_string(),
            latest_version: current.to_string(),
            tag_name: String::new(),
            executable: std::env::current_exe().context("failed to resolve current executable")?,
            updated: false,
        });
    };

    let temp_dir = TempBuilder::new()
        .prefix("codex-termux-update-")
        .tempdir()
        .context("failed to create update temp directory")?;
    let archive_path = temp_dir.path().join(&candidate.asset.name);
    download_asset(&candidate.asset.browser_download_url, &archive_path).await?;

    let extract_dir = temp_dir.path().join("extract");
    tokio::fs::create_dir_all(&extract_dir)
        .await
        .context("failed to create update extraction directory")?;
    extract_archive(&archive_path, &extract_dir).await?;
    let (extracted_codex, extracted_code_mode_host) = find_extracted_binaries(&extract_dir)?;
    let executable = replace_install_binaries(&extracted_codex, &extracted_code_mode_host)?;

    Ok(UpdateOutcome {
        previous_version: current.to_string(),
        latest_version: candidate.version.to_string(),
        tag_name: candidate.tag_name,
        executable,
        updated: true,
    })
}

async fn fetch_releases() -> Result<Vec<GitHubRelease>> {
    Ok(create_client()
        .get(TERMUX_RELEASES_API_URL)
        .send()
        .await?
        .error_for_status()?
        .json::<Vec<GitHubRelease>>()
        .await?)
}

fn ensure_termux_build() -> Result<()> {
    if cfg!(target_os = "android") {
        Ok(())
    } else {
        bail!("codex update is only available in Termux Android builds")
    }
}

fn select_latest_candidate(
    releases: &[GitHubRelease],
    asset_name: &str,
) -> Option<ReleaseCandidate> {
    releases
        .iter()
        .filter_map(|release| candidate_from_release(release, asset_name))
        .max_by(|left, right| left.version.cmp(&right.version))
}

fn select_newer_candidate(
    releases: &[GitHubRelease],
    current: &ReleaseVersion,
    asset_name: &str,
) -> Option<ReleaseCandidate> {
    select_latest_candidate(releases, asset_name)
        .filter(|candidate| candidate.version.cmp(current).is_gt())
}

fn candidate_from_release(release: &GitHubRelease, asset_name: &str) -> Option<ReleaseCandidate> {
    let version = parse_termux_tag(&release.tag_name)?;
    let asset = release
        .assets
        .iter()
        .find(|asset| asset.name == asset_name)?;
    Some(ReleaseCandidate {
        version,
        tag_name: release.tag_name.clone(),
        asset: asset.clone(),
    })
}

fn termux_asset_name() -> String {
    format!("codex-{}-linux-android.tar.gz", termux_arch())
}

fn termux_arch() -> &'static str {
    match std::env::consts::ARCH {
        "aarch64" => "aarch64",
        "x86_64" => "x86_64",
        "arm" => "armv7",
        other => other,
    }
}

async fn download_asset(url: &str, destination: &Path) -> Result<()> {
    let bytes = create_client()
        .get(url)
        .send()
        .await?
        .error_for_status()?
        .bytes()
        .await?;
    tokio::fs::write(destination, bytes)
        .await
        .with_context(|| format!("failed to write update archive {}", destination.display()))
}

async fn extract_archive(archive_path: &Path, destination: &Path) -> Result<()> {
    let status = Command::new("tar")
        .arg("-xzf")
        .arg(archive_path)
        .arg("-C")
        .arg(destination)
        .status()
        .await
        .context("failed to run tar to extract Codex update archive")?;
    if !status.success() {
        bail!("tar failed while extracting Codex update archive: {status}");
    }
    Ok(())
}

fn find_extracted_binaries(root: &Path) -> Result<(PathBuf, PathBuf)> {
    let target_asset_stem = termux_asset_stem();
    let mut stack = vec![(root.to_path_buf(), 0usize)];
    let mut codex = None;
    let mut code_mode_host = None;

    while let Some((dir, depth)) = stack.pop() {
        for entry in fs::read_dir(&dir)
            .with_context(|| format!("failed to read extracted directory {}", dir.display()))?
        {
            let entry = entry?;
            let path = entry.path();
            let file_type = entry.file_type()?;
            if file_type.is_file() {
                let name = entry.file_name();
                if let Some(name) = name.to_str() {
                    if name == "codex-code-mode-host" {
                        code_mode_host = Some(path);
                    } else if name == "codex" || name == target_asset_stem {
                        codex = Some(path);
                    }
                    if let (Some(codex), Some(code_mode_host)) = (&codex, &code_mode_host) {
                        return Ok((codex.clone(), code_mode_host.clone()));
                    }
                }
            } else if file_type.is_dir() && depth < 4 {
                stack.push((path, depth + 1));
            }
        }
    }

    match (codex, code_mode_host) {
        (None, None) => bail!(
            "could not find codex or codex-code-mode-host executables in the downloaded update archive"
        ),
        (None, Some(_)) => {
            bail!("could not find a codex executable in the downloaded update archive")
        }
        (Some(_), None) => bail!(
            "could not find a codex-code-mode-host executable in the downloaded update archive"
        ),
        (Some(codex), Some(code_mode_host)) => Ok((codex, code_mode_host)),
    }
}

fn termux_asset_stem() -> String {
    format!("codex-{}-linux-android", termux_arch())
}

fn replace_install_binaries(
    extracted_codex: &Path,
    extracted_code_mode_host: &Path,
) -> Result<PathBuf> {
    let current_exe = std::env::current_exe().context("failed to resolve current executable")?;
    let install_dir = current_exe
        .parent()
        .context("current executable has no parent directory")?;
    let temp_dir = TempBuilder::new()
        .prefix(".codex-update-")
        .tempdir_in(install_dir)
        .with_context(|| format!("failed to create temp file in {}", install_dir.display()))?;
    let staged_exe = temp_dir.path().join("codex.new");
    let staged_code_mode_host = temp_dir.path().join("codex-code-mode-host.new");
    let code_mode_host = install_dir.join("codex-code-mode-host");

    fs::copy(extracted_codex, &staged_exe).with_context(|| {
        format!(
            "failed to stage downloaded codex binary from {}",
            extracted_codex.display()
        )
    })?;
    let current_permissions = fs::metadata(&current_exe)
        .with_context(|| format!("failed to read metadata for {}", current_exe.display()))?
        .permissions();
    fs::set_permissions(&staged_exe, current_permissions)
        .with_context(|| format!("failed to set permissions on {}", staged_exe.display()))?;
    fs::copy(extracted_code_mode_host, &staged_code_mode_host).with_context(|| {
        format!(
            "failed to stage downloaded codex-code-mode-host binary from {}",
            extracted_code_mode_host.display()
        )
    })?;
    let host_permissions = fs::metadata(&staged_exe)
        .with_context(|| format!("failed to read metadata for {}", staged_exe.display()))?
        .permissions();
    fs::set_permissions(&staged_code_mode_host, host_permissions).with_context(|| {
        format!(
            "failed to set permissions on {}",
            staged_code_mode_host.display()
        )
    })?;
    fs::rename(&staged_code_mode_host, &code_mode_host)
        .with_context(|| format!("failed to replace {}", code_mode_host.display()))?;
    fs::rename(&staged_exe, &current_exe)
        .with_context(|| format!("failed to replace {}", current_exe.display()))?;

    Ok(current_exe)
}

#[cfg(test)]
mod tests {
    use super::*;
    use pretty_assertions::assert_eq;

    fn release(tag_name: &str) -> GitHubRelease {
        GitHubRelease {
            tag_name: tag_name.to_string(),
            assets: vec![GitHubAsset {
                name: "codex-aarch64-linux-android.tar.gz".to_string(),
                browser_download_url: format!("https://example.invalid/{tag_name}.tar.gz"),
            }],
        }
    }

    #[test]
    fn parses_termux_release_tags() {
        assert_eq!(
            parse_termux_tag("rust-v0.141.0-alpha.7-termux")
                .expect("tag should parse")
                .to_string(),
            "0.141.0-alpha.7"
        );
        assert_eq!(
            parse_termux_tag("rust-v0.141.0-termux")
                .expect("tag should parse")
                .to_string(),
            "0.141.0"
        );
        assert_eq!(parse_termux_tag("rust-v0.141.0-alpha.7"), None);
    }

    #[test]
    fn compares_prerelease_versions_against_stable_versions() {
        let alpha_7 = parse_termux_tag("rust-v0.141.0-alpha.7-termux").unwrap();
        let alpha_5 = parse_termux_tag("rust-v0.141.0-alpha.5-termux").unwrap();
        let previous_stable = parse_termux_tag("rust-v0.140.0-termux").unwrap();
        let stable = parse_termux_tag("rust-v0.141.0-termux").unwrap();

        assert!(alpha_7 > alpha_5);
        assert!(alpha_7 > previous_stable);
        assert!(alpha_7 < stable);
    }

    #[test]
    fn selects_latest_compatible_release_by_version() {
        let releases = vec![
            release("rust-v0.141.0-alpha.5-termux"),
            release("rust-v0.140.0-termux"),
            release("rust-v0.141.0-alpha.7-termux"),
            release("rust-v0.141.0-termux"),
        ];

        let candidate =
            select_latest_candidate(&releases, "codex-aarch64-linux-android.tar.gz").unwrap();

        assert_eq!(candidate.tag_name, "rust-v0.141.0-termux");
        assert_eq!(candidate.version.to_string(), "0.141.0");
    }

    #[test]
    fn ignores_releases_without_the_target_artifact() {
        let releases = vec![
            GitHubRelease {
                tag_name: "rust-v0.142.0-termux".to_string(),
                assets: vec![GitHubAsset {
                    name: "codex-x86_64-linux-android.tar.gz".to_string(),
                    browser_download_url: "https://example.invalid/wrong.tar.gz".to_string(),
                }],
            },
            release("rust-v0.141.0-termux"),
        ];

        let candidate =
            select_latest_candidate(&releases, "codex-aarch64-linux-android.tar.gz").unwrap();

        assert_eq!(candidate.tag_name, "rust-v0.141.0-termux");
    }

    #[test]
    fn newer_candidate_requires_version_greater_than_current() {
        let releases = vec![
            release("rust-v0.141.0-alpha.5-termux"),
            release("rust-v0.141.0-alpha.7-termux"),
        ];
        let current = parse_release_version("0.141.0-alpha.7").unwrap();

        assert!(
            select_newer_candidate(&releases, &current, "codex-aarch64-linux-android.tar.gz")
                .is_none()
        );

        let current = parse_release_version("0.141.0-alpha.5").unwrap();
        let candidate =
            select_newer_candidate(&releases, &current, "codex-aarch64-linux-android.tar.gz")
                .unwrap();
        assert_eq!(candidate.tag_name, "rust-v0.141.0-alpha.7-termux");
    }
    #[test]
    fn finds_bundled_termux_binaries() -> Result<()> {
        let root = tempfile::tempdir()?;
        let nested = root.path().join("bundle");
        fs::create_dir(&nested)?;
        let codex = nested.join("codex");
        let code_mode_host = nested.join("codex-code-mode-host");
        fs::write(&codex, "codex")?;
        fs::write(&code_mode_host, "host")?;

        assert_eq!(
            find_extracted_binaries(root.path())?,
            (codex, code_mode_host)
        );
        Ok(())
    }

    #[test]
    fn rejects_update_archive_without_code_mode_host() -> Result<()> {
        let root = tempfile::tempdir()?;
        fs::write(root.path().join("codex"), "codex")?;

        assert_eq!(
            find_extracted_binaries(root.path())
                .expect_err("host should be required")
                .to_string(),
            "could not find a codex-code-mode-host executable in the downloaded update archive"
        );
        Ok(())
    }
}
