#!/usr/bin/env python3

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


ANDROID_GN_PYDEPS_FILES = [
    "build/android/pylib/results/presentation/test_results_presentation.pydeps",
    "build/android/devil_chromium.pydeps",
    "build/android/apk_operations.pydeps",
    "build/android/test_runner.pydeps",
    "build/android/test_wrapper/logdog_wrapper.pydeps",
    "build/android/resource_sizes.pydeps",
]

ANDROID_EXTRA_GN_ARGS = [
    'android_ndk_root="//third_party/android_ndk"',
]


def resolved_v8_crate_version(repo_root: Path) -> str:
    cargo_lock_path = repo_root / "codex-rs" / "Cargo.lock"
    cargo_lock = tomllib.loads(cargo_lock_path.read_text(encoding="utf-8"))
    versions = sorted(
        {
            package["version"]
            for package in cargo_lock["package"]
            if package["name"] == "v8"
        }
    )
    if len(versions) == 1:
        return versions[0]
    if len(versions) > 1:
        raise SystemExit(f"expected exactly one resolved v8 version, found: {versions}")

    cargo_toml_path = repo_root / "codex-rs" / "Cargo.toml"
    if cargo_toml_path.exists():
        matches = sorted(
            set(
                re.findall(
                    r'^v8\s*=\s*"=?([0-9]+\.[0-9]+\.[0-9]+)"',
                    cargo_toml_path.read_text(encoding="utf-8"),
                    flags=re.MULTILINE,
                )
            )
        )
        if len(matches) == 1:
            return matches[0]

    raise SystemExit(f"expected exactly one resolved v8 version in {cargo_lock_path}")


def write_gzip_archive(source_archive: Path, staged_library: Path) -> None:
    with source_archive.open("rb") as src, staged_library.open("wb") as dst:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=dst,
            compresslevel=6,
            mtime=0,
        ) as gz:
            shutil.copyfileobj(src, gz)


def write_checksums(paths: list[Path], checksums_path: Path) -> None:
    with checksums_path.open("w", encoding="utf-8") as checksums:
        for path in paths:
            digest = hashlib.sha256()
            with path.open("rb") as artifact:
                for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                    digest.update(chunk)
            checksums.write(f"{digest.hexdigest()}  {path.name}\n")


def copy_chromium_rust_vendor(vendored_source: Path, rusty_v8_source_root: Path) -> None:
    source_vendor_dir = (
        rusty_v8_source_root / "third_party" / "rust" / "chromium_crates_io" / "vendor"
    )
    if not source_vendor_dir.exists():
        raise SystemExit(f"missing Chromium Rust vendor directory: {source_vendor_dir}")

    dest_vendor_dir = (
        vendored_source / "third_party" / "rust" / "chromium_crates_io" / "vendor"
    )
    if dest_vendor_dir.exists():
        shutil.rmtree(dest_vendor_dir)
    dest_vendor_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_vendor_dir, dest_vendor_dir)


def patch_android_v8_source(vendored_source: Path) -> None:
    stdlib_header = (
        vendored_source / "third_party" / "libc++" / "src" / "include" / "stdlib.h"
    )
    if not stdlib_header.exists():
        raise SystemExit(f"missing libc++ stdlib header: {stdlib_header}")

    text = stdlib_header.read_text(encoding="utf-8")
    replacements = [
        (
            "inline _LIBCPP_HIDE_FROM_ABI ldiv_t div(long __x, long __y) _NOEXCEPT { return ::ldiv(__x, __y); }\n",
            "#if !defined(__ANDROID__)\n"
            "inline _LIBCPP_HIDE_FROM_ABI ldiv_t div(long __x, long __y) _NOEXCEPT { return ::ldiv(__x, __y); }\n"
            "#endif\n",
        ),
        (
            "inline _LIBCPP_HIDE_FROM_ABI lldiv_t div(long long __x, long long __y) _NOEXCEPT { return ::lldiv(__x, __y); }\n",
            "#if !defined(__ANDROID__)\n"
            "inline _LIBCPP_HIDE_FROM_ABI lldiv_t div(long long __x, long long __y) _NOEXCEPT { return ::lldiv(__x, __y); }\n"
            "#endif\n",
        ),
    ]
    for old, new in replacements:
        if old not in text:
            raise SystemExit(
                f"missing expected libc++ Android patch target in {stdlib_header}"
            )
        text = text.replace(old, new, 1)
    stdlib_header.write_text(text, encoding="utf-8")

    abort_message = (
        vendored_source
        / "third_party"
        / "libc++abi"
        / "src"
        / "src"
        / "abort_message.cpp"
    )
    if not abort_message.exists():
        raise SystemExit(f"missing libc++abi abort_message source: {abort_message}")

    text = abort_message.read_text(encoding="utf-8")
    if "#include <stdio.h>" not in text:
        text = text.replace(
            "#include <stdarg.h>\n",
            "#include <stdarg.h>\n#include <stdio.h>\n",
        )
    if "#include <stdlib.h>" not in text:
        text = text.replace(
            "#include <stdio.h>\n",
            "#include <stdio.h>\n#include <stdlib.h>\n",
        )
    abort_message.write_text(text, encoding="utf-8")

    build_rs = vendored_source / "build.rs"
    if not build_rs.exists():
        raise SystemExit(f"missing v8 build script: {build_rs}")

    text = build_rs.read_text(encoding="utf-8")
    old = '  } else if target_os == "linux" {\n'
    new = (
        '  } else if target_os == "android" {\n'
        '    clang_args.push("--target=aarch64-linux-android29".to_string());\n'
        '    clang_args.push("--sysroot=third_party/android_ndk/toolchains/llvm/prebuilt/linux-x86_64/sysroot".to_string());\n'
        '  } else if target_os == "linux" {\n'
    )
    if old not in text:
        raise SystemExit(f"missing expected Android bindgen patch target in {build_rs}")
    build_rs.write_text(text.replace(old, new, 1), encoding="utf-8")


def add_android_extra_gn_args(env: dict[str, str]) -> None:
    existing = " ".join(
        value for value in (env.get("GN_ARGS", ""), env.get("EXTRA_GN_ARGS", "")) if value
    )
    extra_args = [env["EXTRA_GN_ARGS"]] if env.get("EXTRA_GN_ARGS") else []
    for arg in ANDROID_EXTRA_GN_ARGS:
        key = arg.split("=", 1)[0]
        if f"{key}=" not in existing:
            extra_args.append(arg)
    if extra_args:
        env["EXTRA_GN_ARGS"] = " ".join(extra_args)


def vendor_android_v8_crate_source(
    version: str, temp_dir: Path, env: dict[str, str], rusty_v8_source_root: Path
) -> Path:
    cargo_home = Path(env.get("CARGO_HOME", Path.home() / ".cargo")).expanduser()
    candidates = sorted((cargo_home / "registry" / "src").glob(f"*/v8-{version}"))
    if not candidates:
        raise SystemExit(f"missing fetched v8 crate source for {version}")

    vendored_source = temp_dir / f"v8-{version}"
    shutil.copytree(candidates[0], vendored_source)

    # The crates.io v8 147.x source omits Android test-runner pydeps files, but
    # GN reads them while generating Android release build files.
    for relative_path in ANDROID_GN_PYDEPS_FILES:
        pydeps_path = vendored_source / relative_path
        pydeps_path.parent.mkdir(parents=True, exist_ok=True)
        pydeps_path.write_text(
            "# Generated for Android rusty_v8 release staging.\n",
            encoding="utf-8",
        )
    copy_chromium_rust_vendor(vendored_source, rusty_v8_source_root)
    return vendored_source


def install_android_v8_host_sysroot(vendored_source: Path, env: dict[str, str]) -> None:
    subprocess.run(
        [
            sys.executable,
            str(
                vendored_source
                / "build"
                / "linux"
                / "sysroot_scripts"
                / "install-sysroot.py"
            ),
            "--arch=amd64",
        ],
        cwd=vendored_source,
        env=env,
        check=True,
    )


def stage_android_release_pair(
    repo_root: Path, target: str, output_dir: Path, rusty_v8_source_root: Path
) -> None:
    if target != "aarch64-linux-android":
        raise SystemExit(f"unsupported Android rusty_v8 target: {target}")

    version = resolved_v8_crate_version(repo_root)
    temp_dir = Path(tempfile.mkdtemp(prefix="rusty-v8-android-stage-"))
    target_dir = temp_dir / "target"
    manifest_path = temp_dir / "Cargo.toml"
    src_dir = temp_dir / "src"
    src_dir.mkdir()
    manifest_path.write_text(
        "\n".join(
            [
                "[package]",
                'name = "rusty-v8-android-stage"',
                'version = "0.0.0"',
                'edition = "2021"',
                "",
                "[dependencies]",
                f'v8 = "={version}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (src_dir / "lib.rs").write_text(
        "#![allow(dead_code)]\n\npub fn link_v8() {}\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "CARGO_TARGET_DIR": str(target_dir),
        "V8_FROM_SOURCE": "1",
    }
    add_android_extra_gn_args(env)
    subprocess.run(
        [
            "cargo",
            "fetch",
            "--manifest-path",
            str(manifest_path),
            "--target",
            target,
        ],
        cwd=repo_root,
        env=env,
        check=True,
    )
    vendored_source = vendor_android_v8_crate_source(
        version,
        temp_dir,
        env,
        rusty_v8_source_root,
    )
    patch_android_v8_source(vendored_source)
    install_android_v8_host_sysroot(vendored_source, env)
    with manifest_path.open("a", encoding="utf-8") as manifest:
        manifest.write(
            "\n".join(
                [
                    "",
                    "[patch.crates-io]",
                    f'v8 = {{ path = "{vendored_source.as_posix()}" }}',
                    "",
                ]
            )
        )

    subprocess.run(
        [
            "cargo",
            "build",
            "--manifest-path",
            str(manifest_path),
            "--release",
            "--target",
            target,
        ],
        cwd=repo_root,
        env=env,
        check=True,
    )

    build_dir = target_dir / target / "release" / "gn_out"
    archive_path = build_dir / "obj" / "librusty_v8.a"
    binding_path = build_dir / "src_binding.rs"
    if not archive_path.exists():
        raise SystemExit(f"missing Android rusty_v8 archive: {archive_path}")
    if not binding_path.exists():
        raise SystemExit(f"missing Android rusty_v8 binding: {binding_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    staged_library = output_dir / f"librusty_v8_release_{target}.a.gz"
    staged_binding = output_dir / f"src_binding_release_{target}.rs"
    staged_checksums = output_dir / f"rusty_v8_release_{target}.sha256"

    write_gzip_archive(archive_path, staged_library)
    shutil.copyfile(binding_path, staged_binding)
    write_checksums([staged_library, staged_binding], staged_checksums)

    print(staged_library)
    print(staged_binding)
    print(staged_checksums)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolved_version_parser = subparsers.add_parser("resolved-v8-crate-version")
    resolved_version_parser.add_argument("--repo-root", type=Path, required=True)

    stage_parser = subparsers.add_parser("stage-android-release-pair")
    stage_parser.add_argument("--repo-root", type=Path, required=True)
    stage_parser.add_argument("--target", required=True)
    stage_parser.add_argument("--output-dir", type=Path, required=True)
    stage_parser.add_argument("--rusty-v8-source-root", type=Path, required=True)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    if args.command == "resolved-v8-crate-version":
        print(resolved_v8_crate_version(repo_root))
        return 0
    if args.command == "stage-android-release-pair":
        stage_android_release_pair(
            repo_root,
            args.target,
            args.output_dir,
            args.rusty_v8_source_root.resolve(),
        )
        return 0
    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
