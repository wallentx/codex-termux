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

from rusty_v8_module_bazel import (
    RustyV8ChecksumError,
    check_module_bazel,
    update_module_bazel,
)


ROOT = Path(__file__).resolve().parents[2]
MODULE_BAZEL = ROOT / "MODULE.bazel"
RUSTY_V8_CHECKSUMS_DIR = ROOT / "third_party" / "v8"
MUSL_RUNTIME_ARCHIVE_LABELS = [
    "@llvm//runtimes/libcxx:libcxx.static",
    "@llvm//runtimes/libcxx:libcxxabi.static",
]
LLVM_AR_LABEL = "@llvm//tools:llvm-ar"
LLVM_RANLIB_LABEL = "@llvm//tools:llvm-ranlib"
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
    'android_ndk_version="r26c"',
]
ANDROID_BINDGEN_CLANG_ARGS = [
    "--target=aarch64-linux-android29",
    "--sysroot=third_party/android_ndk/toolchains/llvm/prebuilt/linux-x86_64/sysroot",
]


def bazel_execroot() -> Path:
    result = subprocess.run(
        ["bazel", "info", "execution_root"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def bazel_output_base() -> Path:
    result = subprocess.run(
        ["bazel", "info", "output_base"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def bazel_output_path(path: str) -> Path:
    if path.startswith("external/"):
        return bazel_output_base() / path
    return bazel_execroot() / path


def bazel_output_files(
    platform: str,
    labels: list[str],
    compilation_mode: str = "fastbuild",
) -> list[Path]:
    expression = "set(" + " ".join(labels) + ")"
    result = subprocess.run(
        [
            "bazel",
            "cquery",
            "-c",
            compilation_mode,
            f"--platforms=@llvm//platforms:{platform}",
            "--output=files",
            expression,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [bazel_output_path(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def bazel_build(
    platform: str,
    labels: list[str],
    compilation_mode: str = "fastbuild",
) -> None:
    subprocess.run(
        [
            "bazel",
            "build",
            "-c",
            compilation_mode,
            f"--platforms=@llvm//platforms:{platform}",
            *labels,
        ],
        cwd=ROOT,
        check=True,
    )


def ensure_bazel_output_files(
    platform: str,
    labels: list[str],
    compilation_mode: str = "fastbuild",
) -> list[Path]:
    outputs = bazel_output_files(platform, labels, compilation_mode)
    if all(path.exists() for path in outputs):
        return outputs

    bazel_build(platform, labels, compilation_mode)
    outputs = bazel_output_files(platform, labels, compilation_mode)
    missing = [str(path) for path in outputs if not path.exists()]
    if missing:
        raise SystemExit(f"missing built outputs for {labels}: {missing}")
    return outputs


def release_pair_label(target: str) -> str:
    target_suffix = target.replace("-", "_")
    return f"//third_party/v8:rusty_v8_release_pair_{target_suffix}"


def resolved_v8_crate_version() -> str:
    cargo_lock = tomllib.loads((ROOT / "codex-rs" / "Cargo.lock").read_text())
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

    module_bazel = (ROOT / "MODULE.bazel").read_text()
    matches = sorted(
        set(
            re.findall(
                r'https://static\.crates\.io/crates/v8/v8-([0-9]+\.[0-9]+\.[0-9]+)\.crate',
                module_bazel,
            )
        )
    )
    if len(matches) != 1:
        raise SystemExit(
            "expected exactly one pinned v8 crate version in MODULE.bazel, "
            f"found: {matches}"
        )
    return matches[0]


def rusty_v8_checksum_manifest_path(version: str) -> Path:
    return RUSTY_V8_CHECKSUMS_DIR / f"rusty_v8_{version.replace('.', '_')}.sha256"


def command_version(version: str | None) -> str:
    if version is not None:
        return version
    return resolved_v8_crate_version()


def command_manifest_path(manifest: Path | None, version: str) -> Path:
    if manifest is None:
        return rusty_v8_checksum_manifest_path(version)
    if manifest.is_absolute():
        return manifest
    return ROOT / manifest


def staged_archive_name(target: str, source_path: Path) -> str:
    if source_path.suffix == ".lib":
        return f"rusty_v8_release_{target}.lib.gz"
    return f"librusty_v8_release_{target}.a.gz"


def staged_binding_name(target: str) -> str:
    return f"src_binding_release_{target}.rs"


def staged_checksums_name(target: str) -> str:
    return f"rusty_v8_release_{target}.sha256"


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


def add_android_bindgen_clang_args(env: dict[str, str], target: str) -> None:
    key = f"BINDGEN_EXTRA_CLANG_ARGS_{target.replace('-', '_')}"
    bindgen_args = [env[key]] if env.get(key) else []
    existing = env.get(key, "")
    for arg in ANDROID_BINDGEN_CLANG_ARGS:
        if arg not in existing:
            bindgen_args.append(arg)
    env[key] = " ".join(bindgen_args)


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


def is_musl_archive_target(target: str, source_path: Path) -> bool:
    return target.endswith("-unknown-linux-musl") and source_path.suffix == ".a"


def single_bazel_output_file(
    platform: str,
    label: str,
    compilation_mode: str = "fastbuild",
) -> Path:
    outputs = ensure_bazel_output_files(platform, [label], compilation_mode)
    if len(outputs) != 1:
        raise SystemExit(f"expected exactly one output for {label}, found {outputs}")
    return outputs[0]


def merged_musl_archive(
    platform: str,
    lib_path: Path,
    compilation_mode: str = "fastbuild",
) -> Path:
    llvm_ar = single_bazel_output_file(platform, LLVM_AR_LABEL, compilation_mode)
    llvm_ranlib = single_bazel_output_file(platform, LLVM_RANLIB_LABEL, compilation_mode)
    runtime_archives = [
        single_bazel_output_file(platform, label, compilation_mode)
        for label in MUSL_RUNTIME_ARCHIVE_LABELS
    ]

    temp_dir = Path(tempfile.mkdtemp(prefix="rusty-v8-musl-stage-"))
    merged_archive = temp_dir / lib_path.name
    merge_commands = "\n".join(
        [
            f"create {merged_archive}",
            f"addlib {lib_path}",
            *[f"addlib {archive}" for archive in runtime_archives],
            "save",
            "end",
        ]
    )
    subprocess.run(
        [str(llvm_ar), "-M"],
        cwd=ROOT,
        check=True,
        input=merge_commands,
        text=True,
    )
    subprocess.run([str(llvm_ranlib), str(merged_archive)], cwd=ROOT, check=True)
    return merged_archive


def stage_release_pair(
    platform: str,
    target: str,
    output_dir: Path,
    compilation_mode: str = "fastbuild",
) -> None:
    outputs = ensure_bazel_output_files(
        platform,
        [release_pair_label(target)],
        compilation_mode,
    )

    try:
        lib_path = next(path for path in outputs if path.suffix in {".a", ".lib"})
    except StopIteration as exc:
        raise SystemExit(f"missing static library output for {target}") from exc

    try:
        binding_path = next(path for path in outputs if path.suffix == ".rs")
    except StopIteration as exc:
        raise SystemExit(f"missing Rust binding output for {target}") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    staged_library = output_dir / staged_archive_name(target, lib_path)
    staged_binding = output_dir / staged_binding_name(target)
    source_archive = (
        merged_musl_archive(platform, lib_path, compilation_mode)
        if is_musl_archive_target(target, lib_path)
        else lib_path
    )

    write_gzip_archive(source_archive, staged_library)
    shutil.copyfile(binding_path, staged_binding)

    staged_checksums = output_dir / staged_checksums_name(target)
    write_checksums([staged_library, staged_binding], staged_checksums)

    print(staged_library)
    print(staged_binding)
    print(staged_checksums)


def stage_android_release_pair(
    target: str,
    output_dir: Path,
    rusty_v8_source_root: Path,
) -> None:
    if target != "aarch64-linux-android":
        raise SystemExit(f"unsupported Android rusty_v8 target: {target}")

    version = resolved_v8_crate_version()
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
    add_android_bindgen_clang_args(env, target)
    subprocess.run(
        [
            "cargo",
            "fetch",
            "--manifest-path",
            str(manifest_path),
            "--target",
            target,
        ],
        cwd=ROOT,
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
        cwd=ROOT,
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
    staged_library = output_dir / staged_archive_name(target, archive_path)
    staged_binding = output_dir / staged_binding_name(target)
    staged_checksums = output_dir / staged_checksums_name(target)

    write_gzip_archive(archive_path, staged_library)
    shutil.copyfile(binding_path, staged_binding)
    write_checksums([staged_library, staged_binding], staged_checksums)

    print(staged_library)
    print(staged_binding)
    print(staged_checksums)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage_release_pair_parser = subparsers.add_parser("stage-release-pair")
    stage_release_pair_parser.add_argument("--platform", required=True)
    stage_release_pair_parser.add_argument("--target", required=True)
    stage_release_pair_parser.add_argument("--output-dir", required=True)
    stage_release_pair_parser.add_argument(
        "--compilation-mode",
        default="fastbuild",
        choices=["fastbuild", "opt", "dbg"],
    )

    stage_android_release_pair_parser = subparsers.add_parser(
        "stage-android-release-pair"
    )
    stage_android_release_pair_parser.add_argument("--target", required=True)
    stage_android_release_pair_parser.add_argument("--output-dir", required=True)
    stage_android_release_pair_parser.add_argument(
        "--rusty-v8-source-root",
        type=Path,
        required=True,
    )

    subparsers.add_parser("resolved-v8-crate-version")

    check_module_bazel_parser = subparsers.add_parser("check-module-bazel")
    check_module_bazel_parser.add_argument("--version")
    check_module_bazel_parser.add_argument("--manifest", type=Path)
    check_module_bazel_parser.add_argument(
        "--module-bazel",
        type=Path,
        default=MODULE_BAZEL,
    )

    update_module_bazel_parser = subparsers.add_parser("update-module-bazel")
    update_module_bazel_parser.add_argument("--version")
    update_module_bazel_parser.add_argument("--manifest", type=Path)
    update_module_bazel_parser.add_argument(
        "--module-bazel",
        type=Path,
        default=MODULE_BAZEL,
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "stage-release-pair":
        stage_release_pair(
            platform=args.platform,
            target=args.target,
            output_dir=Path(args.output_dir),
            compilation_mode=args.compilation_mode,
        )
        return 0
    if args.command == "stage-android-release-pair":
        stage_android_release_pair(
            target=args.target,
            output_dir=Path(args.output_dir),
            rusty_v8_source_root=args.rusty_v8_source_root.resolve(),
        )
        return 0
    if args.command == "resolved-v8-crate-version":
        print(resolved_v8_crate_version())
        return 0
    if args.command == "check-module-bazel":
        version = command_version(args.version)
        manifest_path = command_manifest_path(args.manifest, version)
        try:
            check_module_bazel(args.module_bazel, manifest_path, version)
        except RustyV8ChecksumError as exc:
            raise SystemExit(str(exc)) from exc
        return 0
    if args.command == "update-module-bazel":
        version = command_version(args.version)
        manifest_path = command_manifest_path(args.manifest, version)
        try:
            update_module_bazel(args.module_bazel, manifest_path, version)
        except RustyV8ChecksumError as exc:
            raise SystemExit(str(exc)) from exc
        return 0
    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
