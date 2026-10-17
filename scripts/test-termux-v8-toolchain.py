#!/usr/bin/env python3
"""Validate the V8 bindgen preflight without running Cargo or loading LLVM."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "rusty_v8_android_release", ROOT / ".github/scripts/rusty_v8_android_release.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AndroidArtifactSelectionTests(unittest.TestCase):
    def run_download_step(self, versions, *, corrupt=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        cargo = root / "codex-rs"
        cargo.mkdir()
        cargo.joinpath("Cargo.lock").write_text(
            'version = 4\n[[package]]\nname = "other"\nversion = "1.0.0"\n'
            + "".join(
                f'[[package]]\nname = "v8"\nversion = "{version}"\n'
                for version in versions
            )
        )
        cargo.joinpath("Cargo.toml").write_text(
            '[workspace.dependencies]\nv8 = "=147.4.0"\n'
        )
        paths = subprocess.check_output(
            [
                "bash",
                "-c",
                'source "$1"; printf "%s\\n" "${TERMUX_RELEASE_GITHUB_SCRIPT_PATHS[@]}"',
                "bash",
                str(ROOT / "scripts/termux-release-paths.sh"),
            ],
            text=True,
        ).splitlines()
        for path in paths:
            destination = root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, destination)
        executable = root / "bin/gh"
        executable.parent.mkdir()
        executable.write_text(
            "#!/usr/bin/env python3\n"
            + textwrap.dedent("""\
                import hashlib
                import json
                import os
                import sys
                from pathlib import Path

                Path(os.environ["DOWNLOAD_ARGS"]).write_text(json.dumps(sys.argv[1:]))
                directory = Path(sys.argv[sys.argv.index("--dir") + 1])
                target = os.environ["TARGET"]
                checksums = []
                for name, data in (
                    (f"librusty_v8_release_{target}.a.gz", b"archive"),
                    (f"src_binding_release_{target}.rs", b"bindings"),
                ):
                    checksums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\\n")
                    if os.environ["CORRUPT"] == "true":
                        data += b"corruption"
                    (directory / name).write_bytes(data)
                (directory / f"rusty_v8_release_{target}.sha256").write_text("".join(checksums))
                """)
        )
        executable.chmod(0o755)
        workflow = (ROOT / ".github/workflows/rust-release.yml").read_text()
        step = workflow.split("name: Configure Android rusty_v8 artifact overrides", 1)[
            1
        ]
        script = textwrap.dedent(step.split("run: |\n", 1)[1].split("\n      -", 1)[0])
        environment = dict(
            os.environ,
            PATH=f"{executable.parent}{os.pathsep}{os.environ['PATH']}",
            RUNNER_TEMP=str(root / "runner"),
            TARGET="aarch64-linux-android",
            GITHUB_ENV=str(root / "environment"),
            DOWNLOAD_ARGS=str(root / "download-args"),
            CORRUPT=str(corrupt).lower(),
        )
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=cargo,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        return result, root

    def test_downloads_the_locked_version_and_exports_the_verified_pair(self):
        for version in ("147.4.0", "152.2.0", "153.0.1"):
            with self.subTest(version=version):
                result, root = self.run_download_step([version])
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = json.loads((root / "download-args").read_text())
                self.assertEqual(
                    arguments[:3], ["release", "download", f"rusty-v8-v{version}"]
                )
                artifact_root = root / "runner/rusty_v8"
                self.assertEqual(
                    (root / "environment").read_text().splitlines(),
                    [
                        f"RUSTY_V8_ARCHIVE={artifact_root}/librusty_v8_release_aarch64-linux-android.a.gz",
                        f"RUSTY_V8_SRC_BINDING_PATH={artifact_root}/src_binding_release_aarch64-linux-android.rs",
                    ],
                )

    def test_ambiguous_versions_fail_before_downloading_or_exporting(self):
        result, root = self.run_download_step(["147.4.0", "152.2.0"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected exactly one resolved v8 version", result.stderr)
        self.assertFalse((root / "download-args").exists())
        self.assertFalse((root / "environment").exists())

    def test_bad_checksums_fail_before_exporting_artifact_paths(self):
        result, root = self.run_download_step(["152.2.0"], corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED", result.stdout)
        self.assertFalse((root / "environment").exists())


class BindgenToolchainTests(unittest.TestCase):
    def test_supported_versions_use_selected_library_and_dispose_string(self):
        for version in (b"Ubuntu clang version 21.1.8", b"clang version 22.1.0"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as directory,
            ):
                library = Mock()
                library.clang_getCString.return_value = version
                with patch.object(MODULE.ctypes, "CDLL", return_value=library) as load:
                    MODULE.validate_android_bindgen_toolchain(
                        {"LIBCLANG_PATH": directory}
                    )
                load.assert_called_once_with(str(Path(directory) / "libclang.so"))
                library.clang_disposeString.assert_called_once_with(
                    library.clang_getClangVersion.return_value
                )

    def test_rejects_old_and_unrecognized_versions(self):
        for version in (
            b"Ubuntu clang version 19.1.7",
            b"clang version 21.0.0",
            b"unknown",
            None,
        ):
            with self.subTest(version=version):
                library = Mock()
                library.clang_getCString.return_value = version
                with (
                    patch.object(MODULE.ctypes, "CDLL", return_value=library),
                    self.assertRaisesRegex(SystemExit, r"require libclang 21\.1\+"),
                ):
                    MODULE.validate_android_bindgen_toolchain(
                        {"LIBCLANG_PATH": "/toolchain/libclang.so"}
                    )
                library.clang_disposeString.assert_called_once()

    def test_accepts_explicit_library_file(self):
        library = Mock()
        library.clang_getCString.return_value = b"clang version 21.1.0"
        with patch.object(MODULE.ctypes, "CDLL", return_value=library) as load:
            MODULE.validate_android_bindgen_toolchain(
                {"LIBCLANG_PATH": "/toolchain/libclang.so"}
            )
        load.assert_called_once_with("/toolchain/libclang.so")

    def test_requires_explicit_library_path(self):
        with self.assertRaisesRegex(SystemExit, "requires LIBCLANG_PATH"):
            MODULE.validate_android_bindgen_toolchain({})

    def test_unloadable_library_has_actionable_error(self):
        with (
            patch.object(MODULE.ctypes, "CDLL", side_effect=OSError("missing library")),
            self.assertRaisesRegex(SystemExit, "cannot load bindgen libclang"),
        ):
            MODULE.validate_android_bindgen_toolchain(
                {"LIBCLANG_PATH": "/missing/libclang.so"}
            )

    def test_non_libclang_library_is_rejected(self):
        with (
            patch.object(MODULE.ctypes, "CDLL", return_value=object()),
            self.assertRaisesRegex(SystemExit, "cannot load bindgen libclang"),
        ):
            MODULE.validate_android_bindgen_toolchain(
                {"LIBCLANG_PATH": "/wrong/library.so"}
            )

    def test_failed_preflight_runs_before_cargo_or_temp_workspace(self):
        with (
            patch.object(
                MODULE,
                "validate_android_bindgen_toolchain",
                side_effect=SystemExit("old libclang"),
            ),
            patch.object(MODULE.subprocess, "run") as run,
            patch.object(MODULE.tempfile, "mkdtemp") as create_workspace,
            self.assertRaisesRegex(SystemExit, "old libclang"),
        ):
            MODULE.stage_android_release_pair(
                Path("source"), "aarch64-linux-android", Path("dist"), Path("v8")
            )
        run.assert_not_called()
        create_workspace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
